import { useEffect, useMemo, useRef, useState } from "react";
import {
  Copy,
  Eraser,
  Move,
  Pencil,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import {
  CatalogMap,
  DEFAULT_LAYOUT,
  ObjectKind,
  OfficeLayout,
  Placement,
  SeatDir,
  TileObject,
  defaultZ,
} from "./catalog";
import {
  CompiledScene,
  TILE,
  compileLayout,
  drawPlacement,
  drawSeatChair,
} from "./engine";

const ATLAS_URLS = {
  room: { url: "/office/room.png", cols: 16, rows: 14 },
  furniture: { url: "/office/furniture.png", cols: 16, rows: 53 },
} as const;

interface Props {
  layout: OfficeLayout;
  setLayout: (l: OfficeLayout) => void;
  catalog: CatalogMap;
  customCatalog: CatalogMap;
  setCustomCatalog: (c: CatalogMap) => void;
  // Resolves the latest catalog+layout to the server. Called when the
  // user clicks Save. Promise resolves on success, rejects on failure.
  onPersist: () => Promise<void>;
  zoom: number;
  showToast?: (text: string, kind?: "info" | "error") => void;
}

// Pending new-object draft — populated when the user drag-selects in an
// atlas. Saved entries land in `customCatalog`.
interface NewObjectDraft {
  atlas: "room" | "furniture";
  sx: number;
  sy: number;
  w: number; // in tile units
  h: number;
}

type Tool = "place" | "erase" | "move";

export function OfficeEditor({
  layout,
  setLayout,
  catalog,
  customCatalog,
  setCustomCatalog,
  onPersist,
  zoom,
  showToast,
}: Props) {
  const [tool, setTool] = useState<Tool>("place");
  const [stagedId, setStagedId] = useState<string | null>(null);
  const [hover, setHover] = useState<
    | { kind: "grid"; x: number; y: number }
    | { kind: "atlas"; atlas: string; col: number; row: number; sx: number; sy: number }
    | null
  >(null);
  const [dirty, setDirty] = useState(false);
  // Drag-select state on the atlas: tracks start/current cell in atlas
  // tile coords. Resolved into a NewObjectDraft on mouseup.
  const [drag, setDrag] = useState<
    | {
        atlas: "room" | "furniture";
        startCol: number;
        startRow: number;
        endCol: number;
        endRow: number;
      }
    | null
  >(null);
  const [draft, setDraft] = useState<NewObjectDraft | null>(null);

  const scene: CompiledScene = useMemo(
    () => compileLayout(layout, catalog),
    [layout, catalog],
  );
  const BG_W = scene.cols * TILE;
  const BG_H = scene.rows * TILE;

  const gridCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const atlasRefs = useRef<Record<string, HTMLCanvasElement | null>>({});
  const atlasImagesRef = useRef<Record<string, HTMLImageElement>>({});
  // Atlas readiness lives in state, not a ref: child components (catalog
  // tile previews, the new-object dialog) read it via prop and rely on
  // useEffect deps to redraw when an atlas finishes loading. A ref mutation
  // doesn't trigger that, which is why thumbnails stayed gray.
  const [atlasReady, setAtlasReady] = useState<Record<string, boolean>>({});

  // Preload atlases once. Images live on a ref (stable identity); their
  // load completion flips the `atlasReady` state and triggers re-renders.
  useEffect(() => {
    for (const [name, meta] of Object.entries(ATLAS_URLS)) {
      if (atlasImagesRef.current[name]) continue;
      const img = new Image();
      img.src = meta.url;
      atlasImagesRef.current[name] = img;
      img.onload = () => {
        setAtlasReady((prev) => (prev[name] ? prev : { ...prev, [name]: true }));
      };
    }
  }, []);

  // Paint the office grid in edit mode: floor checker + placements + grid
  // lines + hover highlight. Re-runs on every state change that changes
  // what's visible.
  function paintGrid() {
    const canvas = gridCanvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;

    ctx.fillStyle = "#1d1f24";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.save();
    ctx.scale(zoom, zoom);

    // Floor (checker fallback).
    for (let y = 0; y < scene.rows; y++) {
      for (let x = 0; x < scene.cols; x++) {
        ctx.fillStyle = (x + y) % 2 === 0 ? "#d3d6dc" : "#c8ccd2";
        ctx.fillRect(x * TILE, y * TILE, TILE, TILE);
      }
    }

    // Render order: stable sort by `obj.z` (with kind-derived default),
    // then by row so back rows render before front. Lets a desk sit on
    // top of a floor placement and a chair render above a desk in the
    // same cell without anyone wiping anyone.
    const sorted = [...scene.placements].sort((a, b) => {
      const za = a.obj.z ?? defaultZ(a.obj.kind);
      const zb = b.obj.z ?? defaultZ(b.obj.kind);
      if (za !== zb) return za - zb;
      return a.y - b.y;
    });
    for (const cp of sorted) {
      drawPlacement(ctx, cp, atlasImagesRef.current, atlasReady);
    }
    for (const seat of scene.seats) {
      drawSeatChair(ctx, seat, atlasImagesRef.current, atlasReady);
    }

    // Outline every placement so a sprite that visually matches the
    // background (e.g. a floor-coloured fragment) still shows in edit
    // mode. Floor outlines are dimmer than furniture; the placement
    // currently held by the Move tool gets a brighter yellow ring.
    ctx.lineWidth = 1 / zoom;
    const selectedPlacement =
      selectedIdx != null ? layout.placements[selectedIdx] : null;
    scene.placements.forEach((cp) => {
      const isSelected =
        selectedPlacement != null &&
        cp.id === selectedPlacement.id &&
        cp.x === selectedPlacement.x &&
        cp.y === selectedPlacement.y;
      if (isSelected) {
        ctx.strokeStyle = "rgba(255, 215, 0, 0.95)";
        ctx.lineWidth = 2 / zoom;
      } else {
        ctx.strokeStyle =
          cp.obj.kind === "floor"
            ? "rgba(56, 139, 253, 0.30)"
            : "rgba(56, 139, 253, 0.85)";
        ctx.lineWidth = 1 / zoom;
      }
      ctx.strokeRect(
        cp.x * TILE + 0.5 / zoom,
        cp.y * TILE + 0.5 / zoom,
        cp.w * TILE - 1 / zoom,
        cp.h * TILE - 1 / zoom,
      );
    });

    // Grid lines.
    ctx.strokeStyle = "rgba(0, 0, 0, 0.12)";
    ctx.lineWidth = 1 / zoom;
    for (let x = 0; x <= scene.cols; x++) {
      ctx.beginPath();
      ctx.moveTo(x * TILE, 0);
      ctx.lineTo(x * TILE, BG_H);
      ctx.stroke();
    }
    for (let y = 0; y <= scene.rows; y++) {
      ctx.beginPath();
      ctx.moveTo(0, y * TILE);
      ctx.lineTo(BG_W, y * TILE);
      ctx.stroke();
    }

    // Walkability overlay on placement-blocked cells. With no implicit
    // outer-ring wall, every red square here corresponds to something the
    // user explicitly placed.
    ctx.fillStyle = "rgba(255, 80, 80, 0.18)";
    for (let y = 0; y < scene.rows; y++) {
      for (let x = 0; x < scene.cols; x++) {
        if (!scene.walkable[y][x]) ctx.fillRect(x * TILE, y * TILE, TILE, TILE);
      }
    }

    // Hover highlight.
    if (hover?.kind === "grid") {
      ctx.fillStyle =
        tool === "erase"
          ? "rgba(248, 81, 73, 0.45)"
          : "rgba(63, 185, 80, 0.45)";
      ctx.fillRect(hover.x * TILE, hover.y * TILE, TILE, TILE);
    }

    ctx.restore();
  }

  function paintAtlas(name: keyof typeof ATLAS_URLS) {
    const canvas = atlasRefs.current[name];
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const img = atlasImagesRef.current[name];
    ctx.imageSmoothingEnabled = false;
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (img && atlasReady[name]) {
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    }
    // Grid overlay.
    const meta = ATLAS_URLS[name];
    const cellW = canvas.width / meta.cols;
    const cellH = canvas.height / meta.rows;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.08)";
    ctx.lineWidth = 1;
    for (let c = 0; c <= meta.cols; c++) {
      ctx.beginPath();
      ctx.moveTo(c * cellW, 0);
      ctx.lineTo(c * cellW, canvas.height);
      ctx.stroke();
    }
    for (let r = 0; r <= meta.rows; r++) {
      ctx.beginPath();
      ctx.moveTo(0, r * cellH);
      ctx.lineTo(canvas.width, r * cellH);
      ctx.stroke();
    }
    // Hover.
    if (hover?.kind === "atlas" && hover.atlas === name) {
      ctx.fillStyle = "rgba(63, 185, 80, 0.35)";
      ctx.fillRect(hover.col * cellW, hover.row * cellH, cellW, cellH);
    }
    // Drag rectangle: selected region while user drags.
    if (drag && drag.atlas === name) {
      const c0 = Math.min(drag.startCol, drag.endCol);
      const r0 = Math.min(drag.startRow, drag.endRow);
      const c1 = Math.max(drag.startCol, drag.endCol);
      const r1 = Math.max(drag.startRow, drag.endRow);
      ctx.fillStyle = "rgba(56, 139, 253, 0.25)";
      ctx.fillRect(
        c0 * cellW,
        r0 * cellH,
        (c1 - c0 + 1) * cellW,
        (r1 - r0 + 1) * cellH,
      );
      ctx.strokeStyle = "rgba(56, 139, 253, 0.9)";
      ctx.lineWidth = 2;
      ctx.strokeRect(
        c0 * cellW,
        r0 * cellH,
        (c1 - c0 + 1) * cellW,
        (r1 - r0 + 1) * cellH,
      );
    }
  }

  useEffect(paintGrid);
  useEffect(() => {
    paintAtlas("room");
    paintAtlas("furniture");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hover, drag, zoom, atlasReady]);

  // Releasing the button anywhere on the page ends the paint stroke —
  // otherwise the stroke "sticks" if the user lifts off the canvas.
  useEffect(() => {
    const onUp = () => stopPainting();
    window.addEventListener("mouseup", onUp);
    return () => window.removeEventListener("mouseup", onUp);
  }, []);

  // --- Mouse handlers.

  const gridCellAt = (
    e: React.MouseEvent<HTMLCanvasElement>,
  ): { x: number; y: number } | null => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.floor((e.clientX - rect.left) / (TILE * zoom));
    const y = Math.floor((e.clientY - rect.top) / (TILE * zoom));
    if (x < 0 || y < 0 || x >= scene.cols || y >= scene.rows) return null;
    return { x, y };
  };

  const onGridMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = gridCellAt(e);
    if (!cell) {
      if (hover?.kind === "grid") setHover(null);
      return;
    }
    if (
      hover?.kind !== "grid" ||
      hover.x !== cell.x ||
      hover.y !== cell.y
    ) {
      setHover({ kind: "grid", x: cell.x, y: cell.y });
    }
    // Continue the drag-paint stroke onto the new cell.
    if (paintingRef.current) {
      const key = `${cell.x},${cell.y}`;
      if (lastPaintedRef.current === key) return;
      lastPaintedRef.current = key;
      if (paintingRef.current === "place") placeAt(cell.x, cell.y);
      else eraseAt(cell.x, cell.y);
    }
  };

  const placeAt = (x: number, y: number) => {
    if (!stagedId) {
      showToast?.("Pick an object first", "error");
      return;
    }
    const obj = catalog[stagedId];
    if (!obj) return;
    const w = obj.w ?? 1;
    const h = obj.h ?? 1;
    if (x + w > layout.cols || y + h > layout.rows) {
      showToast?.("Object doesn't fit there", "error");
      return;
    }
    const next: Placement[] = layout.placements.filter((p) => {
      const o = catalog[p.id];
      if (!o) return false;
      const pw = o.w ?? 1;
      const ph = o.h ?? 1;
      const overlap =
        p.x < x + w && p.x + pw > x && p.y < y + h && p.y + ph > y;
      if (!overlap) return true;
      // Only replace placements of the same kind. Desks, walls, decor,
      // chairs, floors, etc. each occupy independent slots in a cell —
      // putting a desk on a partition won't wipe the partition.
      return o.kind !== obj.kind;
    });
    next.push({ id: stagedId, x, y });
    setLayout({ ...layout, placements: next });
    setDirty(true);
  };

  // Pick the topmost placement that contains (x, y), iterating in reverse
  // so a stacked desk-on-floor returns the desk first.
  const placementIndexAt = (x: number, y: number): number | null => {
    for (let i = layout.placements.length - 1; i >= 0; i--) {
      const p = layout.placements[i];
      const o = catalog[p.id];
      if (!o) continue;
      const w = o.w ?? 1;
      const h = o.h ?? 1;
      if (x >= p.x && x < p.x + w && y >= p.y && y < p.y + h) return i;
    }
    return null;
  };

  const moveSelectedTo = (x: number, y: number) => {
    if (selectedIdx == null) return;
    const p = layout.placements[selectedIdx];
    if (!p) return;
    const o = catalog[p.id];
    if (!o) return;
    const w = o.w ?? 1;
    const h = o.h ?? 1;
    if (x + w > layout.cols || y + h > layout.rows || x < 0 || y < 0) {
      showToast?.("Object wouldn't fit there", "error");
      return;
    }
    const next = layout.placements.slice();
    next[selectedIdx] = { ...p, x, y };
    setLayout({ ...layout, placements: next });
    setDirty(true);
  };

  const eraseAt = (x: number, y: number) => {
    const next = layout.placements.filter((p) => {
      const o = catalog[p.id];
      const pw = o?.w ?? 1;
      const ph = o?.h ?? 1;
      const hit = x >= p.x && x < p.x + pw && y >= p.y && y < p.y + ph;
      return !hit;
    });
    if (next.length === layout.placements.length) return;
    setLayout({ ...layout, placements: next });
    setDirty(true);
  };

  const onGridMouseDown = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = gridCellAt(e);
    if (!cell) return;
    // Move tool: click a placement → pick it up; click elsewhere → drop.
    // Right-click in move mode still erases (matches Place/Erase tools).
    if (tool === "move" && e.button !== 2) {
      const idx = placementIndexAt(cell.x, cell.y);
      if (selectedIdx == null) {
        if (idx != null) setSelectedIdx(idx);
        return;
      }
      // Already holding something: click on the target cell drops it.
      // If the clicked cell hosts another placement of the same kind,
      // the standard placeAt-style conflict rule applies on commit (we
      // don't auto-swap; user can pick up the conflict and move it next).
      moveSelectedTo(cell.x, cell.y);
      setSelectedIdx(null);
      return;
    }
    const mode: "place" | "erase" =
      e.button === 2 || tool === "erase" ? "erase" : "place";
    paintingRef.current = mode;
    lastPaintedRef.current = `${cell.x},${cell.y}`;
    if (mode === "place") placeAt(cell.x, cell.y);
    else eraseAt(cell.x, cell.y);
  };

  const stopPainting = () => {
    paintingRef.current = null;
    lastPaintedRef.current = null;
  };

  const atlasCellAt = (
    name: keyof typeof ATLAS_URLS,
    e: React.MouseEvent<HTMLCanvasElement>,
  ): { col: number; row: number } | null => {
    const canvas = e.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const meta = ATLAS_URLS[name];
    const cellW = canvas.width / meta.cols;
    const cellH = canvas.height / meta.rows;
    const col = Math.floor(
      ((e.clientX - rect.left) * canvas.width) / rect.width / cellW,
    );
    const row = Math.floor(
      ((e.clientY - rect.top) * canvas.height) / rect.height / cellH,
    );
    if (col < 0 || row < 0 || col >= meta.cols || row >= meta.rows) return null;
    return { col, row };
  };

  const onAtlasMove = (
    name: keyof typeof ATLAS_URLS,
    e: React.MouseEvent<HTMLCanvasElement>,
  ) => {
    const cell = atlasCellAt(name, e);
    if (!cell) {
      if (hover?.kind === "atlas") setHover(null);
      return;
    }
    setHover({
      kind: "atlas",
      atlas: name,
      col: cell.col,
      row: cell.row,
      sx: cell.col * 16,
      sy: cell.row * 16,
    });
    if (drag && drag.atlas === name) {
      setDrag({ ...drag, endCol: cell.col, endRow: cell.row });
    }
  };

  const onAtlasMouseDown = (
    name: keyof typeof ATLAS_URLS,
    e: React.MouseEvent<HTMLCanvasElement>,
  ) => {
    if (e.button !== 0) return;
    const cell = atlasCellAt(name, e);
    if (!cell) return;
    setDrag({
      atlas: name,
      startCol: cell.col,
      startRow: cell.row,
      endCol: cell.col,
      endRow: cell.row,
    });
  };

  const onAtlasMouseUp = (
    name: keyof typeof ATLAS_URLS,
    e: React.MouseEvent<HTMLCanvasElement>,
  ) => {
    if (!drag || drag.atlas !== name) return;
    const cell = atlasCellAt(name, e);
    if (!cell) {
      setDrag(null);
      return;
    }
    const col0 = Math.min(drag.startCol, cell.col);
    const row0 = Math.min(drag.startRow, cell.row);
    const col1 = Math.max(drag.startCol, cell.col);
    const row1 = Math.max(drag.startRow, cell.row);
    setDrag(null);
    setDraft({
      atlas: name,
      sx: col0 * 16,
      sy: row0 * 16,
      w: col1 - col0 + 1,
      h: row1 - row0 + 1,
    });
  };

  // --- Toolbar actions.

  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Index into layout.placements for the placement the Move tool has
  // "picked up". null in any other tool or when nothing is selected.
  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);
  // Catalog filters. Defaults to "floor" so the first thing the user sees
  // is floors (matches the typical "lay the floor first" workflow).
  const [categoryFilter, setCategoryFilter] = useState<ObjectKind | "all">(
    "floor",
  );
  const [searchText, setSearchText] = useState("");
  // Drag-to-paint state on the office grid: holding the mouse button down
  // keeps placing/erasing as the cursor moves over new cells. Tracked via
  // refs (no re-render needed per cell visited).
  const paintingRef = useRef<"place" | "erase" | null>(null);
  const lastPaintedRef = useRef<string | null>(null);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onPersist();
      setDirty(false);
      showToast?.("Saved to project file");
    } catch (err) {
      showToast?.((err as Error).message || "Save failed", "error");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = (id: string) => {
    const isCustom = !!customCatalog[id] && !customCatalog[id].hidden;
    const label = isCustom
      ? `Delete custom object "${id}"?`
      : `Hide built-in object "${id}" from catalog?`;
    if (!confirm(label)) return;
    const next = { ...customCatalog };
    if (isCustom) {
      // Remove the custom override outright.
      delete next[id];
    } else {
      // Plant a tombstone so the built-in falls out of the merged catalog
      // until the user removes the tombstone (= "restore built-in").
      next[id] = { id, kind: "decor", hidden: true };
    }
    setCustomCatalog(next);
    // Drop any placements that referenced the now-gone id.
    const placements = layout.placements.filter((p) => p.id !== id);
    if (placements.length !== layout.placements.length) {
      setLayout({ ...layout, placements });
    }
    if (stagedId === id) setStagedId(null);
    setDirty(true);
  };

  const handleEditCustom = (id: string) => {
    if (!customCatalog[id]) {
      showToast?.("Only custom objects are editable", "error");
      return;
    }
    setEditingId(id);
  };
  const handleReset = () => {
    setLayout(DEFAULT_LAYOUT);
    setDirty(true);
  };
  const handleExport = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(layout, null, 2));
      showToast?.("Layout JSON copied");
    } catch {
      showToast?.("Clipboard write blocked", "error");
    }
  };
  const handleClearAll = () => {
    setLayout({ ...layout, placements: [] });
    setDirty(true);
  };

  // Catalog preview rendering — small thumbnails of each object. Custom
  // entries get delete/edit affordances; built-ins are read-only here
  // (live in catalog.ts under source control).
  // Apply category + search filters to the merged catalog. Search runs
  // case-insensitive against the title (or id when title is unset).
  const filteredCatalog = useMemo(() => {
    const needle = searchText.trim().toLowerCase();
    return Object.values(catalog).filter((obj) => {
      if (categoryFilter !== "all" && obj.kind !== categoryFilter) return false;
      if (!needle) return true;
      const hay = (obj.title ?? obj.id).toLowerCase();
      return hay.includes(needle);
    });
  }, [catalog, categoryFilter, searchText]);

  const renderCatalogPreview = (obj: TileObject) => {
    const size = 36;
    const isCustom = !!customCatalog[obj.id];
    return (
      <CatalogPreview
        key={obj.id}
        obj={obj}
        size={size}
        active={stagedId === obj.id}
        isCustom={isCustom}
        atlasImages={atlasImagesRef.current}
        atlasReady={atlasReady}
        onClick={() => setStagedId(obj.id)}
        onEdit={() => handleEditCustom(obj.id)}
        onDelete={() => handleDelete(obj.id)}
      />
    );
  };

  return (
    <div className="office-editor">
      <div className="office-editor-toolbar">
        <button
          type="button"
          className={`with-icon${tool === "place" ? " active" : ""}`}
          onClick={() => {
            setTool("place");
            setSelectedIdx(null);
          }}
          title="Place tool (left-click)"
        >
          Place
        </button>
        <button
          type="button"
          className={`with-icon${tool === "erase" ? " active" : ""}`}
          onClick={() => {
            setTool("erase");
            setSelectedIdx(null);
          }}
          title="Erase tool"
        >
          <Eraser size={14} />
          <span className="btn-label">Erase</span>
        </button>
        <button
          type="button"
          className={`with-icon${tool === "move" ? " active" : ""}`}
          onClick={() => setTool("move")}
          title="Move tool — click a placement to pick it up, click again to drop"
        >
          <Move size={14} />
          <span className="btn-label">Move</span>
        </button>
        <button type="button" className="with-icon" onClick={handleClearAll} title="Clear all placements">
          <Trash2 size={14} />
          <span className="btn-label">Clear</span>
        </button>
        <button
          type="button"
          className={`with-icon${dirty ? " active" : ""}`}
          onClick={handleSave}
          disabled={saving}
          title="Save catalog + layout to project file"
        >
          <Save size={14} />
          <span className="btn-label">{saving ? "Saving…" : "Save"}</span>
        </button>
        <button type="button" className="with-icon" onClick={handleReset} title="Restore default layout">
          <RotateCcw size={14} />
          <span className="btn-label">Reset</span>
        </button>
        <button type="button" className="with-icon" onClick={handleExport} title="Copy layout JSON">
          <Copy size={14} />
          <span className="btn-label">Export</span>
        </button>
        <div className="office-editor-status">
          {tool === "move" ? (
            <span className="office-editor-status-staged">
              {selectedIdx == null
                ? "Move mode — click a placement to pick up"
                : `Holding placement #${selectedIdx} — click target cell to drop`}
            </span>
          ) : stagedId ? (
            <span className="office-editor-status-staged">
              {tool === "erase" ? "Eraser mode" : `Click grid to place: ${stagedId}`}
            </span>
          ) : (
            <span className="office-editor-status-empty">
              Pick a catalog tile → click grid to place
            </span>
          )}
          {` · ${layout.placements.length} placements`}
          {hover?.kind === "grid" && ` · (${hover.x}, ${hover.y})`}
          {hover?.kind === "atlas" &&
            ` · ${hover.atlas} sx=${hover.sx} sy=${hover.sy}`}
        </div>
      </div>
      <div className="office-editor-body">
        <canvas
          ref={gridCanvasRef}
          width={BG_W * zoom}
          height={BG_H * zoom}
          className="office-editor-canvas"
          style={{
            width: `${BG_W * zoom}px`,
            height: `${BG_H * zoom}px`,
            imageRendering: "pixelated",
          }}
          onMouseMove={onGridMove}
          onMouseLeave={() => setHover(null)}
          onMouseDown={onGridMouseDown}
          onContextMenu={(e) => {
            // Right-click both prevents the browser menu and starts an
            // erase stroke immediately.
            e.preventDefault();
            onGridMouseDown(e as unknown as React.MouseEvent<HTMLCanvasElement>);
          }}
        />
        <div className="office-editor-sidebar">
          <div className="office-editor-section">
            <h4>Catalog</h4>
            <p className="office-editor-hint">
              Click a tile to select · click (or hold-drag) on the office
              grid to paint. Right-click / drag erases. ✎ edits, 🗑 deletes
              (built-ins are hidden, not destroyed).
            </p>
            <div className="office-editor-catalog-filters">
              <select
                value={categoryFilter}
                onChange={(e) =>
                  setCategoryFilter(e.target.value as ObjectKind | "all")
                }
                aria-label="Category"
              >
                <option value="all">All</option>
                <option value="floor">Floor</option>
                <option value="wall">Wall</option>
                <option value="desk">Desk</option>
                <option value="chair">Chair</option>
                <option value="plant">Plant</option>
                <option value="decor">Decor</option>
              </select>
              <input
                type="search"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                placeholder="Search by title…"
              />
            </div>
            <div className="office-editor-catalog">
              {filteredCatalog.length === 0 ? (
                <div className="office-editor-empty">
                  Nothing in "{categoryFilter}". Drag-select a region in
                  an atlas below to create one.
                </div>
              ) : (
                filteredCatalog.map(renderCatalogPreview)
              )}
            </div>
          </div>
          <div className="office-editor-section">
            <h4>Atlas: room — drag to create object</h4>
            <canvas
              ref={(el) => (atlasRefs.current.room = el)}
              width={ATLAS_URLS.room.cols * 16 * 2}
              height={ATLAS_URLS.room.rows * 16 * 2}
              className="office-editor-atlas"
              onMouseMove={(e) => onAtlasMove("room", e)}
              onMouseLeave={() => setHover(null)}
              onMouseDown={(e) => onAtlasMouseDown("room", e)}
              onMouseUp={(e) => onAtlasMouseUp("room", e)}
            />
          </div>
          <div className="office-editor-section">
            <h4>Atlas: furniture — drag to create object</h4>
            <canvas
              ref={(el) => (atlasRefs.current.furniture = el)}
              width={ATLAS_URLS.furniture.cols * 16 * 2}
              height={ATLAS_URLS.furniture.rows * 16 * 2}
              className="office-editor-atlas"
              onMouseMove={(e) => onAtlasMove("furniture", e)}
              onMouseLeave={() => setHover(null)}
              onMouseDown={(e) => onAtlasMouseDown("furniture", e)}
              onMouseUp={(e) => onAtlasMouseUp("furniture", e)}
            />
          </div>
        </div>
      </div>
      {draft && (
        <NewObjectDialog
          draft={draft}
          existingIds={Object.keys(catalog)}
          atlasImages={atlasImagesRef.current}
          atlasReady={atlasReady}
          onCancel={() => setDraft(null)}
          onSave={(obj) => {
            const next = { ...customCatalog, [obj.id]: obj };
            setCustomCatalog(next);
            setStagedId(obj.id);
            setDraft(null);
            setDirty(true);
            showToast?.(`Created "${obj.id}"`);
          }}
        />
      )}
      {editingId && customCatalog[editingId] && (
        <NewObjectDialog
          existing={customCatalog[editingId]}
          existingIds={Object.keys(catalog).filter((k) => k !== editingId)}
          atlasImages={atlasImagesRef.current}
          atlasReady={atlasReady}
          onCancel={() => setEditingId(null)}
          onSave={(obj) => {
            const next = { ...customCatalog };
            // If the user renamed the object, drop the old id.
            if (obj.id !== editingId) delete next[editingId];
            next[obj.id] = obj;
            setCustomCatalog(next);
            // Rewrite placements pointing at the old id.
            if (obj.id !== editingId) {
              const placements = layout.placements.map((p) =>
                p.id === editingId ? { ...p, id: obj.id } : p,
              );
              setLayout({ ...layout, placements });
            }
            setStagedId(obj.id);
            setEditingId(null);
            setDirty(true);
            showToast?.(`Updated "${obj.id}"`);
          }}
        />
      )}
    </div>
  );
}

interface NewObjectDialogProps {
  draft?: NewObjectDraft;
  existing?: TileObject;
  existingIds: string[];
  atlasImages: Record<string, HTMLImageElement>;
  atlasReady: Record<string, boolean>;
  onSave: (obj: TileObject) => void;
  onCancel: () => void;
}

function NewObjectDialog({
  draft,
  existing,
  existingIds,
  atlasImages,
  atlasReady,
  onSave,
  onCancel,
}: NewObjectDialogProps) {
  // Initial values come from `existing` (edit mode) or `draft` (create mode).
  const initialSprite = existing?.sprite;
  // The id is stable once chosen: new objects get a UUID, edits keep
  // theirs. Users only see/edit `title` — the human-readable label.
  const [id] = useState(() => existing?.id ?? generateId());
  const [title, setTitle] = useState(() => existing?.title ?? "");
  const [kind, setKind] = useState<ObjectKind>(existing?.kind ?? "decor");
  const [blocksOverride, setBlocksOverride] = useState<boolean | null>(
    existing?.blocks ?? null,
  );
  const [hasSeat, setHasSeat] = useState(!!existing?.workSeat);
  const [seatDx, setSeatDx] = useState(existing?.workSeat?.dx ?? 0);
  const [seatDy, setSeatDy] = useState(existing?.workSeat?.dy ?? 1);
  const [seatDir, setSeatDir] = useState<SeatDir>(
    existing?.workSeat?.dir ?? "up",
  );
  const [seatPrimary, setSeatPrimary] = useState(
    !!existing?.workSeat?.primary,
  );
  const [hasRest, setHasRest] = useState(!!existing?.restSeat);
  const [restDx, setRestDx] = useState(existing?.restSeat?.dx ?? 0);
  const [restDy, setRestDy] = useState(existing?.restSeat?.dy ?? 1);
  const [restDir, setRestDir] = useState<SeatDir>(
    existing?.restSeat?.dir ?? "up",
  );
  // Optional z override. Empty string = "use kind-derived default".
  const [zText, setZText] = useState<string>(
    existing?.z != null ? String(existing.z) : "",
  );
  // Pixel-precise render offset. Empty = 0.
  const [offsetXText, setOffsetXText] = useState<string>(
    existing?.offsetX != null ? String(existing.offsetX) : "",
  );
  const [offsetYText, setOffsetYText] = useState<string>(
    existing?.offsetY != null ? String(existing.offsetY) : "",
  );
  const collision = blocksOverride ?? defaultBlocksByKind(kind);
  // id collisions can't happen on save with UUIDs, but the safety net
  // stays so future code (manual id schemes, imports) still validates.
  const idTaken = existingIds.includes(id);
  const canSave = !idTaken;

  // Resolve the sprite + footprint to use. Edit mode keeps the existing
  // sprite; create mode pulls from the drag-selected draft.
  const atlasLabel = draft?.atlas ?? initialSprite?.atlas ?? "—";
  const sx = draft?.sx ?? initialSprite?.sx ?? 0;
  const sy = draft?.sy ?? initialSprite?.sy ?? 0;
  const w = draft?.w ?? existing?.w ?? 1;
  const h = draft?.h ?? existing?.h ?? 1;

  // Per-cell collision mask. Defaults to "block everything" unless the
  // user edits an existing object that already specifies a mask.
  const [mask, setMask] = useState<boolean[][]>(() => {
    if (existing?.collisionMask) {
      // Deep-clone and pad to current w/h.
      const out: boolean[][] = [];
      for (let y = 0; y < h; y++) {
        const row: boolean[] = [];
        for (let x = 0; x < w; x++) {
          row.push(existing.collisionMask[y]?.[x] ?? true);
        }
        out.push(row);
      }
      return out;
    }
    return Array.from({ length: h }, () =>
      Array.from({ length: w }, () => true),
    );
  });

  const toggleMaskCell = (col: number, row: number) => {
    setMask((m) => {
      const next = m.map((r) => [...r]);
      next[row][col] = !next[row][col];
      return next;
    });
  };
  const resetMask = (val: boolean) => {
    setMask(
      Array.from({ length: h }, () => Array.from({ length: w }, () => val)),
    );
  };

  // Render the preview canvas — sprite from atlas with red overlay on
  // blocking cells. Re-runs whenever mask or atlas readiness changes.
  const previewRef = useRef<HTMLCanvasElement | null>(null);
  const PREVIEW_TILE = 36;
  useEffect(() => {
    const canvas = previewRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#0d1117";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const atlasName = draft?.atlas ?? existing?.sprite?.atlas;
    const atlas = atlasName ? atlasImages[atlasName] : undefined;
    const ready = atlasName ? atlasReady[atlasName] : false;
    if (atlas && ready) {
      ctx.drawImage(
        atlas,
        sx,
        sy,
        w * 16,
        h * 16,
        0,
        0,
        w * PREVIEW_TILE,
        h * PREVIEW_TILE,
      );
    }
    // Grid + collision overlay.
    for (let row = 0; row < h; row++) {
      for (let col = 0; col < w; col++) {
        const cx = col * PREVIEW_TILE;
        const cy = row * PREVIEW_TILE;
        if (mask[row]?.[col]) {
          ctx.fillStyle = "rgba(248, 81, 73, 0.40)";
          ctx.fillRect(cx, cy, PREVIEW_TILE, PREVIEW_TILE);
        }
        ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
        ctx.strokeRect(cx + 0.5, cy + 0.5, PREVIEW_TILE - 1, PREVIEW_TILE - 1);
      }
    }
  }, [mask, atlasImages, atlasReady, draft, existing, sx, sy, w, h]);

  const onPreviewClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const col = Math.floor(((e.clientX - rect.left) / rect.width) * w);
    const row = Math.floor(((e.clientY - rect.top) / rect.height) * h);
    if (col < 0 || row < 0 || col >= w || row >= h) return;
    toggleMaskCell(col, row);
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSave) return;
    const obj: TileObject = {
      id,
      kind,
      w,
      h,
      blocks: collision,
    };
    if (title.trim()) obj.title = title.trim();
    if (existing?.sprite && !draft) {
      obj.sprite = existing.sprite;
    } else if (draft) {
      obj.sprite = {
        atlas: draft.atlas,
        sx: draft.sx,
        sy: draft.sy,
        sw: draft.w * 16,
        sh: draft.h * 16,
      };
    }
    // Persist mask only if it deviates from "all cells block" — keeps the
    // JSON tidy and matches the engine's default-true semantics.
    const allBlock = mask.every((row) => row.every((c) => c));
    if (collision && !allBlock) {
      obj.collisionMask = mask;
    }
    if (hasSeat) {
      obj.workSeat = { dx: seatDx, dy: seatDy, dir: seatDir };
      if (seatPrimary) obj.workSeat.primary = true;
    }
    if (hasRest) {
      obj.restSeat = { dx: restDx, dy: restDy, dir: restDir };
    }
    const zParsed = zText.trim() === "" ? null : Number(zText);
    if (zParsed != null && Number.isFinite(zParsed)) {
      obj.z = zParsed;
    }
    const oxParsed =
      offsetXText.trim() === "" ? null : Number(offsetXText);
    if (oxParsed != null && Number.isFinite(oxParsed) && oxParsed !== 0) {
      obj.offsetX = oxParsed;
    }
    const oyParsed =
      offsetYText.trim() === "" ? null : Number(offsetYText);
    if (oyParsed != null && Number.isFinite(oyParsed) && oyParsed !== 0) {
      obj.offsetY = oyParsed;
    }
    onSave(obj);
  };

  return (
    <div className="office-modal-backdrop" onClick={onCancel}>
      <form
        className="office-modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
      >
        <header className="office-modal-header">
          <strong>{existing ? "Edit catalog object" : "New catalog object"}</strong>
          <button
            type="button"
            className="icon-button"
            onClick={onCancel}
            aria-label="Close"
          >
            <X size={14} />
          </button>
        </header>
        <div className="office-modal-body">
          <div className="office-modal-row">
            <label>
              <span>Title</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                autoFocus
                placeholder={existing ? "" : "e.g. Wood desk, Tall plant…"}
              />
              {idTaken && (
                <small className="office-modal-err">id collision</small>
              )}
            </label>
            <label>
              <span>Kind</span>
              <select value={kind} onChange={(e) => setKind(e.target.value as ObjectKind)}>
                <option value="floor">floor</option>
                <option value="wall">wall</option>
                <option value="desk">desk</option>
                <option value="chair">chair</option>
                <option value="plant">plant</option>
                <option value="decor">decor</option>
              </select>
            </label>
          </div>
          <div className="office-modal-row office-modal-id-row">
            <small>id: <code>{id}</code></small>
          </div>
          <div className="office-modal-row">
            <label>
              <span>Atlas</span>
              <input value={atlasLabel} readOnly />
            </label>
            <label>
              <span>sx, sy</span>
              <input value={`${sx}, ${sy}`} readOnly />
            </label>
            <label>
              <span>w × h (tiles)</span>
              <input value={`${w} × ${h}`} readOnly />
            </label>
          </div>
          {collision && (
            <div className="office-modal-mask">
              <div className="office-modal-mask-label">
                <span>Collision per cell — click red = blocks, gray = walkable</span>
                <div className="office-modal-mask-buttons">
                  <button type="button" onClick={() => resetMask(true)}>
                    All block
                  </button>
                  <button type="button" onClick={() => resetMask(false)}>
                    Clear
                  </button>
                </div>
              </div>
              <canvas
                ref={previewRef}
                width={w * PREVIEW_TILE}
                height={h * PREVIEW_TILE}
                onClick={onPreviewClick}
                className="office-modal-mask-canvas"
                style={{ imageRendering: "pixelated" }}
              />
            </div>
          )}
          <div className="office-modal-row">
            <label className="office-modal-check">
              <input
                type="checkbox"
                checked={collision}
                onChange={(e) => setBlocksOverride(e.target.checked)}
              />
              <span>Blocks movement</span>
            </label>
            <label className="office-modal-check">
              <input
                type="checkbox"
                checked={hasSeat}
                onChange={(e) => setHasSeat(e.target.checked)}
              />
              <span>Has work seat</span>
            </label>
            <label className="office-modal-check">
              <input
                type="checkbox"
                checked={hasRest}
                onChange={(e) => setHasRest(e.target.checked)}
              />
              <span>Has rest seat</span>
            </label>
            <label>
              <span>z-index (blank = auto from kind)</span>
              <input
                value={zText}
                onChange={(e) => setZText(e.target.value)}
                placeholder="auto"
                inputMode="numeric"
              />
            </label>
          </div>
          <div className="office-modal-row">
            <label>
              <span>offset X (px, visual only)</span>
              <input
                value={offsetXText}
                onChange={(e) => setOffsetXText(e.target.value)}
                placeholder="0"
                inputMode="numeric"
              />
            </label>
            <label>
              <span>offset Y (px, visual only)</span>
              <input
                value={offsetYText}
                onChange={(e) => setOffsetYText(e.target.value)}
                placeholder="0"
                inputMode="numeric"
              />
            </label>
          </div>
          {hasSeat && (
            <div className="office-modal-row">
              <label>
                <span>work seat dx</span>
                <input
                  type="number"
                  value={seatDx}
                  onChange={(e) => setSeatDx(Number(e.target.value))}
                />
              </label>
              <label>
                <span>work seat dy</span>
                <input
                  type="number"
                  value={seatDy}
                  onChange={(e) => setSeatDy(Number(e.target.value))}
                />
              </label>
              <label>
                <span>facing</span>
                <select
                  value={seatDir}
                  onChange={(e) => setSeatDir(e.target.value as SeatDir)}
                >
                  <option value="up">up</option>
                  <option value="down">down</option>
                  <option value="left">left</option>
                  <option value="right">right</option>
                </select>
              </label>
              <label className="office-modal-check">
                <input
                  type="checkbox"
                  checked={seatPrimary}
                  onChange={(e) => setSeatPrimary(e.target.checked)}
                />
                <span>Primary (main agent picks first)</span>
              </label>
            </div>
          )}
          {hasRest && (
            <div className="office-modal-row">
              <label>
                <span>rest seat dx</span>
                <input
                  type="number"
                  value={restDx}
                  onChange={(e) => setRestDx(Number(e.target.value))}
                />
              </label>
              <label>
                <span>rest seat dy</span>
                <input
                  type="number"
                  value={restDy}
                  onChange={(e) => setRestDy(Number(e.target.value))}
                />
              </label>
              <label>
                <span>facing</span>
                <select
                  value={restDir}
                  onChange={(e) => setRestDir(e.target.value as SeatDir)}
                >
                  <option value="up">up</option>
                  <option value="down">down</option>
                  <option value="left">left</option>
                  <option value="right">right</option>
                </select>
              </label>
            </div>
          )}
        </div>
        <footer className="office-modal-footer">
          <button type="button" onClick={onCancel} className="with-icon">
            Cancel
          </button>
          <button
            type="submit"
            className="with-icon active"
            disabled={!canSave}
          >
            <Plus size={14} />
            <span className="btn-label">{existing ? "Update" : "Create"}</span>
          </button>
        </footer>
      </form>
    </div>
  );
}

// Stable random id for new catalog entries. Uses Crypto.randomUUID when
// available (every browser we target supports it). The fallback path
// keeps the editor working in any obscure runtime that doesn't.
function generateId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `obj_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function defaultBlocksByKind(kind: ObjectKind): boolean {
  return kind !== "floor" && kind !== "chair";
}

interface CatalogPreviewProps {
  obj: TileObject;
  size: number;
  active: boolean;
  isCustom: boolean;
  atlasImages: Record<string, HTMLImageElement>;
  atlasReady: Record<string, boolean>;
  onClick: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

function CatalogPreview({
  obj,
  size,
  active,
  isCustom,
  atlasImages,
  atlasReady,
  onClick,
  onEdit,
  onDelete,
}: CatalogPreviewProps) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, c.width, c.height);
    const w = obj.w ?? 1;
    const h = obj.h ?? 1;
    const cell = Math.min(c.width / w, c.height / h);
    const offX = (c.width - cell * w) / 2;
    const offY = (c.height - cell * h) / 2;
    // Use the engine's draw path against a temp coord (0,0) at proper scale.
    ctx.save();
    ctx.translate(offX, offY);
    ctx.scale(cell / 16, cell / 16);
    const sprite = obj.sprite;
    if (sprite) {
      const atlas = atlasImages[sprite.atlas];
      if (atlas && atlasReady[sprite.atlas]) {
        const sw = sprite.sw ?? w * 16;
        const sh = sprite.sh ?? h * 16;
        ctx.drawImage(atlas, sprite.sx, sprite.sy, sw, sh, 0, 0, w * 16, h * 16);
      } else {
        ctx.fillStyle = obj.fallbackColor ?? "#888";
        ctx.fillRect(0, 0, w * 16, h * 16);
      }
    } else {
      // Re-use the engine's primitive renderer via a transient placement.
      const cp = {
        id: obj.id,
        x: 0,
        y: 0,
        obj,
        w,
        h,
      };
      drawPlacement(ctx, cp, atlasImages, atlasReady);
    }
    ctx.restore();
  }, [obj, size, atlasImages, atlasReady]);
  return (
    <div
      className={`catalog-tile-wrap${active ? " active" : ""}${isCustom ? " custom" : ""}`}
    >
      <button
        type="button"
        className="catalog-tile"
        onClick={onClick}
        title={`${obj.title ?? obj.id} · ${obj.kind} · id=${obj.id}`}
      >
        <canvas ref={ref} width={size} height={size} />
        <span className="catalog-tile-label">{obj.title ?? obj.id}</span>
      </button>
      <div className="catalog-tile-actions">
        {isCustom && (
          <button
            type="button"
            className="icon-button"
            onClick={(e) => {
              e.stopPropagation();
              onEdit();
            }}
            title="Edit object"
            aria-label="Edit"
          >
            <Pencil size={11} />
          </button>
        )}
        <button
          type="button"
          className="icon-button"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          title={isCustom ? "Delete custom object" : "Hide built-in from catalog"}
          aria-label="Delete"
        >
          <Trash2 size={11} />
        </button>
      </div>
    </div>
  );
}
