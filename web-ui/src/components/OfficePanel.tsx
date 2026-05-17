import { useEffect, useMemo, useRef, useState } from "react";
import { Pencil, Users, X } from "lucide-react";
import { WsEvent } from "../api";
import { api } from "../api";
import {
  CatalogMap,
  DEFAULT_LAYOUT,
  OfficeLayout,
  TileObject,
  defaultZ,
  mergeCatalog,
} from "./office/catalog";
import {
  CompiledScene,
  Dir,
  DIR_DOWN,
  DIR_UP,
  TILE,
  compileLayout,
  dirFromVec,
  drawPlacement,
  drawPlacementCell,
  drawSeatChair,
  findNearestWalkable,
  findPath,
  pickWanderTarget,
} from "./office/engine";
import { OfficeEditor } from "./office/Editor";

const ICON = 16;

const ATLAS_URLS = {
  room: "/office/room.png",
  furniture: "/office/furniture.png",
} as const;

const CHAR_URLS = [
  "/office/char_adam.png",
  "/office/char_alex.png",
  "/office/char_amelia.png",
  "/office/char_bob.png",
] as const;

// Short human-readable status pulled from the FSM state. Keep these
// terse — the badge sits in a 16-pixel grid so a wide string makes the
// scene unreadable.
function statusLabel(state: Sprite["state"]): string {
  switch (state) {
    case "wander":
      return "Idle";
    case "going_to_work":
      return "→ Desk";
    case "working":
      return "Working";
    case "going_to_idle":
      return "→ Floor";
    case "going_to_rest":
      return "→ Lounge";
    case "resting":
      return "Resting";
    case "leaving_rest":
      return "↑";
    default:
      return state;
  }
}

function statusColor(state: Sprite["state"]): string {
  switch (state) {
    case "working":
      return "#3fb950";
    case "going_to_work":
      return "#8ed074";
    case "resting":
      return "#f0883e";
    case "going_to_rest":
      return "#fab57e";
    default:
      return "#9aa4b2";
  }
}

// Stable string → small int hash. Used to pick a character sprite from a
// session id or tool-use id so the same agent always renders as the same
// character across re-renders, but different agents distribute across the
// 4 LimeZu models.
function hashSprite(id: string, slots: number): number {
  let h = 2166136261; // FNV-1a 32-bit seed
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % slots);
}

const CHAR_W = 16;
const CHAR_H = 32;
const FRAMES_PER_DIR = 6;

interface OfficeAgent {
  id: string;
  name: string;
  role: "main" | "sub";
  busy: boolean;
  spriteIdx: number;
}

interface Sprite {
  agent: OfficeAgent;
  x: number;
  y: number;
  path: Array<[number, number]>;
  state:
    | "wander"
    | "going_to_work"
    | "working"
    | "going_to_idle"
    | "going_to_rest"
    | "resting"
    | "leaving_rest";
  // Work-seat reservation (when busy). Mutually exclusive with restSeatIdx.
  seatIdx: number | null;
  // Rest-spot reservation (idle wandering → relax). Cleared when the
  // agent stands up or gets interrupted by a `busy` transition.
  restSeatIdx: number | null;
  // Timestamp (ms) when the agent should stand up from the rest seat.
  restUntil: number;
  // Soonest ts the agent is allowed to consider resting again. Pumped
  // each time they leave a rest seat to stop ping-pong loitering.
  restCooldownUntil: number;
  dir: Dir;
  animTime: number;
  isMoving: boolean;
  nextDecisionAt: number;
}

interface Props {
  windowId: string;
  sessionName: string;
  busy: boolean;
  open: boolean;
  onClose: () => void;
  subscribeWs: (l: (e: WsEvent) => void) => () => void;
  showToast?: (text: string, kind?: "info" | "error") => void;
}

export function OfficePanel({
  windowId,
  sessionName,
  busy,
  showToast,
  open,
  onClose,
  subscribeWs,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Fixed scene scale + no debug overlay in runtime view; toggles for
  // both used to live in the panel header but cluttered the UI.
  const zoom = 2;
  const debug = false;
  const [editing, setEditing] = useState(false);
  const [layout, setLayout] = useState<OfficeLayout>(DEFAULT_LAYOUT);
  const [customCatalog, setCustomCatalog] = useState<CatalogMap>({});

  // Asset loading state. Images live on stable refs; readiness flips in
  // React state so re-renders pick it up (a ref mutation alone wouldn't
  // re-trigger the render effect, and the overlay needs to disappear
  // exactly when the last sheet finishes loading).
  const atlasImagesRef = useRef<Record<string, HTMLImageElement>>({});
  const [atlasReady, setAtlasReady] = useState<Record<string, boolean>>({});
  const charSheetsRef = useRef<HTMLImageElement[]>([]);
  const [charReady, setCharReady] = useState<boolean[]>(
    () => CHAR_URLS.map(() => false),
  );
  useEffect(() => {
    for (const [name, url] of Object.entries(ATLAS_URLS)) {
      if (atlasImagesRef.current[name]) continue;
      const img = new Image();
      img.src = url;
      atlasImagesRef.current[name] = img;
      img.onload = () => {
        setAtlasReady((prev) =>
          prev[name] ? prev : { ...prev, [name]: true },
        );
      };
    }
    CHAR_URLS.forEach((url, i) => {
      if (charSheetsRef.current[i]) return;
      const img = new Image();
      img.src = url;
      charSheetsRef.current[i] = img;
      img.onload = () => {
        setCharReady((prev) => {
          if (prev[i]) return prev;
          const next = prev.slice();
          next[i] = true;
          return next;
        });
      };
    });
  }, []);
  const assetsReady =
    Object.keys(ATLAS_URLS).every((n) => atlasReady[n]) &&
    charReady.every(Boolean);
  const [subAgents, setSubAgents] = useState<Map<string, string>>(
    () => new Map(),
  );

  // Hydrate from the server on mount. Until this resolves we show
  // DEFAULT_LAYOUT so the panel isn't blank, but we don't push back to
  // the server until the user explicitly saves.
  useEffect(() => {
    let cancelled = false;
    api
      .getOfficeState()
      .then((state) => {
        if (cancelled) return;
        if (state.layout) {
          setLayout(state.layout as OfficeLayout);
        }
        const restored: CatalogMap = {};
        for (const [key, value] of Object.entries(state.catalog ?? {})) {
          restored[key] = value as TileObject;
        }
        setCustomCatalog(restored);
      })
      .catch(() => {
        // 401 or network: keep defaults; user can still edit, save will
        // surface the error.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Explicit save → POST current state to server.
  const persistState = async () => {
    await api.putOfficeState({
      catalog: customCatalog as unknown as Record<string, unknown>,
      layout: layout as unknown as { cols: number; rows: number; placements: unknown[] },
    });
  };

  // Merged catalog: built-in objects + user-added. Custom entries shadow
  // built-ins on id collision.
  const catalog = useMemo(
    () => mergeCatalog(customCatalog),
    [customCatalog],
  );

  // Compile the user's layout against the merged catalog. Re-compiles when
  // either changes; runtime view picks up edits automatically.
  const scene: CompiledScene = useMemo(
    () => compileLayout(layout, catalog),
    [layout, catalog],
  );
  const BG_W = scene.cols * TILE;
  const BG_H = scene.rows * TILE;

  useEffect(() => {
    setSubAgents(new Map());
  }, [windowId]);

  useEffect(() => {
    const unsub = subscribeWs((event) => {
      if (!("window_id" in event) || event.window_id !== windowId) return;
      if (event.type !== "message") {
        if (event.type === "completion") {
          // Safety net: end-of-turn clears any sub-agent whose
          // tool_result we somehow missed.
          setSubAgents((prev) => (prev.size === 0 ? prev : new Map()));
        }
        return;
      }
      const id = event.tool_use_id;
      if (!id) return;
      // tool_use → spawn a worker for this in-flight tool. tool_result
      // → that specific tool finished, retire its worker. The JSONL
      // monitor surfaces both with matching tool_use_id, so the lifecycle
      // is exact (one in / one out) without timers.
      if (event.content_type === "tool_use" && event.tool_name) {
        const name = event.tool_name;
        setSubAgents((prev) => {
          if (prev.has(id)) return prev;
          const next = new Map(prev);
          next.set(id, name);
          return next;
        });
      } else if (event.content_type === "tool_result") {
        setSubAgents((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Map(prev);
          next.delete(id);
          return next;
        });
      }
    });
    return unsub;
  }, [windowId, subscribeWs]);

  useEffect(() => {
    if (!busy && subAgents.size > 0) {
      setSubAgents(new Map());
    }
  }, [busy, subAgents.size]);

  const mainAgent: OfficeAgent = {
    id: `main:${windowId}`,
    name: sessionName,
    role: "main",
    busy,
    spriteIdx: hashSprite(windowId, CHAR_URLS.length),
  };
  const subAgentList: OfficeAgent[] = Array.from(subAgents.entries()).map(
    ([id, name]) => ({
      id: `sub:${id}`,
      name,
      role: "sub",
      busy: true,
      spriteIdx: hashSprite(id, CHAR_URLS.length),
    }),
  );
  const agents = [mainAgent, ...subAgentList];

  const spritesRef = useRef<Map<string, Sprite>>(new Map());

  useEffect(() => {
    const current = spritesRef.current;
    const seen = new Set<string>();
    for (const a of agents) {
      seen.add(a.id);
      const existing = current.get(a.id);
      if (existing) {
        existing.agent = a;
        if (
          a.busy &&
          (existing.state === "wander" ||
            existing.state === "going_to_rest" ||
            existing.state === "resting" ||
            existing.state === "leaving_rest")
        ) {
          // Becoming busy interrupts any rest-related state. Release the
          // rest seat reservation immediately so another idle agent can
          // claim it without waiting for the tick FSM to notice.
          existing.restSeatIdx = null;
          existing.state = "going_to_work";
          existing.path = [];
          existing.nextDecisionAt = 0;
        }
        if (!a.busy && existing.state === "working") {
          existing.state = "going_to_idle";
          existing.path = [];
          existing.nextDecisionAt = 0;
        }
      } else {
        // Spawn on a walkable tile. pickWanderTarget already prefers
        // walkable cells, but its absolute fallback is (0, 0) which
        // could itself be blocked; bouncing through findNearestWalkable
        // guarantees a safe spawn (or null when the entire grid is full,
        // in which case the per-tick escape logic in `tick` handles it).
        const [rx, ry] = pickWanderTarget(scene);
        const safe = findNearestWalkable(scene, rx, ry) ?? [rx, ry];
        current.set(a.id, {
          agent: a,
          x: safe[0],
          y: safe[1],
          path: [],
          state: "wander",
          seatIdx: null,
          restSeatIdx: null,
          restUntil: 0,
          restCooldownUntil: 0,
          dir: DIR_DOWN,
          animTime: 0,
          isMoving: false,
          nextDecisionAt: 0,
        });
      }
    }
    for (const id of current.keys()) {
      if (!seen.has(id)) current.delete(id);
    }
  }, [agents, scene]);

  useEffect(() => {
    // Only run the animation loop in view mode — the editor has its own
    // (static) renderer and the agents are paused while editing.
    if (!open || editing) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.imageSmoothingEnabled = false;

    // Assets live on the component's stable refs/state. We just borrow
    // them locally so the render loop reads consistent values.
    const atlases = atlasImagesRef.current;
    const charSheets = charSheetsRef.current;

    let rafId = 0;
    let lastTs = performance.now();

    const drawCharacter = (
      spriteIdx: number,
      dir: Dir,
      frame: number,
      px: number,
      py: number,
    ) => {
      const sheet = charSheets[spriteIdx];
      if (!sheet || !charReady[spriteIdx]) return;
      const frameInDir =
        ((frame % FRAMES_PER_DIR) + FRAMES_PER_DIR) % FRAMES_PER_DIR;
      const sx = (dir * FRAMES_PER_DIR + frameInDir) * CHAR_W;
      ctx.drawImage(sheet, sx, 0, CHAR_W, CHAR_H, px, py - TILE, CHAR_W, CHAR_H);
    };

    const occupiedSeats = (): Set<number> => {
      const set = new Set<number>();
      for (const s of spritesRef.current.values()) {
        if (s.seatIdx != null) set.add(s.seatIdx);
      }
      return set;
    };

    const occupiedRestSeats = (): Set<number> => {
      const set = new Set<number>();
      for (const s of spritesRef.current.values()) {
        if (s.restSeatIdx != null) set.add(s.restSeatIdx);
      }
      return set;
    };

    // How often an idle wanderer decides to head for a rest seat instead
    // of a random tile. Lower → less lounging.
    const REST_CHANCE = 0.12;
    // Random duration an agent stays seated before standing up again.
    const REST_MIN_MS = 4000;
    const REST_MAX_MS = 9000;
    // After leaving a rest seat, agents wait this long before being
    // eligible to rest again — otherwise an idle agent ping-pongs
    // between sofa and wander forever.
    const REST_COOLDOWN_MS = 15000;

    const tick = (ts: number) => {
      const dt = Math.min(50, ts - lastTs) / 1000;
      lastTs = ts;

      const taken = occupiedSeats();
      const takenRest = occupiedRestSeats();

      for (const s of spritesRef.current.values()) {
        // Escape if standing inside a non-walkable cell — happens when a
        // placement edit blocked the cell underneath the agent, or when
        // a layout reload spawned them in a tight spot. Teleport to the
        // closest walkable tile and drop any in-flight path so the next
        // decision tick re-plans cleanly.
        const cx = Math.round(s.x);
        const cy = Math.round(s.y);
        if (!scene.walkable[cy]?.[cx]) {
          const escape = findNearestWalkable(scene, cx, cy);
          if (escape) {
            s.x = escape[0];
            s.y = escape[1];
            s.path = [];
            s.isMoving = false;
            s.nextDecisionAt = 0;
            // If they were working at a seat that no longer makes sense,
            // free it and head back to wandering on the next decision.
            if (s.seatIdx != null) {
              s.seatIdx = null;
              s.state = "wander";
            }
          }
        }
        if (s.path.length > 0) {
          const [tx, ty] = s.path[0];
          const dx = tx - s.x;
          const dy = ty - s.y;
          const dist = Math.hypot(dx, dy);
          const speed = 2.5;
          if (dist <= speed * dt) {
            s.x = tx;
            s.y = ty;
            s.path.shift();
            s.isMoving = s.path.length > 0;
          } else {
            s.x += (dx / dist) * speed * dt;
            s.y += (dy / dist) * speed * dt;
            s.dir = dirFromVec(dx, dy);
            s.isMoving = true;
          }
        } else {
          s.isMoving = false;
        }

        if (s.isMoving) s.animTime += dt;

        if (s.path.length === 0 && ts >= s.nextDecisionAt) {
          if (s.state === "wander") {
            // Idle wanderers occasionally head for a free rest seat
            // instead of a random floor tile.
            const freeRest = scene.restSeats
              .map((_, i) => i)
              .filter((i) => !takenRest.has(i));
            if (
              freeRest.length > 0 &&
              ts >= s.restCooldownUntil &&
              Math.random() < REST_CHANCE
            ) {
              const idx = freeRest[Math.floor(Math.random() * freeRest.length)];
              const rs = scene.restSeats[idx];
              const path = findPath(
                scene,
                Math.round(s.x),
                Math.round(s.y),
                rs.x,
                rs.y,
              );
              if (path.length > 0) {
                s.restSeatIdx = idx;
                takenRest.add(idx);
                s.path = path;
                s.state = "going_to_rest";
                s.nextDecisionAt = ts + 200;
                continue;
              }
            }
            const [nx, ny] = pickWanderTarget(scene);
            s.path = findPath(scene, Math.round(s.x), Math.round(s.y), nx, ny);
            s.nextDecisionAt = ts + 600 + Math.random() * 1200;
          } else if (s.state === "going_to_rest") {
            // Arrived at the rest seat → sit down for a random spell.
            if (s.restSeatIdx == null) {
              s.state = "wander";
              s.nextDecisionAt = 0;
              continue;
            }
            const rs = scene.restSeats[s.restSeatIdx];
            s.dir = rs.dir;
            s.state = "resting";
            s.restUntil =
              ts + REST_MIN_MS + Math.random() * (REST_MAX_MS - REST_MIN_MS);
            s.nextDecisionAt = s.restUntil;
          } else if (s.state === "resting") {
            // Sit time elapsed; stand up.
            s.state = "leaving_rest";
            s.nextDecisionAt = 0;
          } else if (s.state === "leaving_rest") {
            if (s.restSeatIdx != null) {
              takenRest.delete(s.restSeatIdx);
              s.restSeatIdx = null;
            }
            s.restCooldownUntil = ts + REST_COOLDOWN_MS;
            s.state = "wander";
            s.nextDecisionAt = 0;
          } else if (s.state === "going_to_work") {
            if (s.seatIdx == null) {
              const free = scene.seats
                .map((_, i) => i)
                .filter((i) => !taken.has(i));
              if (free.length === 0) {
                s.state = "wander";
                s.nextDecisionAt = 0;
                continue;
              }
              // Main agent grabs a primary seat when one is available.
              // Sub-agents (or main when none is marked primary) just
              // get a random free seat.
              const primaryFree =
                s.agent.role === "main"
                  ? free.filter((i) => scene.seats[i].primary)
                  : [];
              const pool = primaryFree.length > 0 ? primaryFree : free;
              const idx = pool[Math.floor(Math.random() * pool.length)];
              s.seatIdx = idx;
              taken.add(idx);
              const seat = scene.seats[idx];
              s.path = findPath(
                scene,
                Math.round(s.x),
                Math.round(s.y),
                seat.x,
                seat.y,
              );
              if (s.path.length === 0) {
                s.seatIdx = null;
                s.nextDecisionAt = ts + 500;
              } else {
                s.nextDecisionAt = ts + 200;
              }
            } else {
              const seat = scene.seats[s.seatIdx];
              s.dir = seat.dir;
              s.state = "working";
              s.nextDecisionAt = ts + 1000;
            }
          } else if (s.state === "working") {
            s.nextDecisionAt = ts + 1000;
          } else if (s.state === "going_to_idle") {
            if (s.seatIdx != null) {
              taken.delete(s.seatIdx);
              s.seatIdx = null;
            }
            s.state = "wander";
            s.nextDecisionAt = 0;
          }
        }
      }

      // --- Draw scene. Canvas starts fully transparent each frame so
      // empty cells reveal whatever CSS background sits beneath the
      // canvas — no fake floor placeholder bleeds through.
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      ctx.save();
      ctx.scale(zoom, zoom);

      // No floor checker in runtime view — empty cells stay transparent
      // over the panel's dark background. The checker is an editor-only
      // aid for placement.
      // All placements sorted by z (kind-derived default) then by row,
      // so layers stack naturally (floor → walls → desks/decor → chairs).
      const sortedPlacements = [...scene.placements].sort((a, b) => {
        const za = a.obj.z ?? defaultZ(a.obj.kind);
        const zb = b.obj.z ?? defaultZ(b.obj.kind);
        if (za !== zb) return za - zb;
        return a.y - b.y;
      });
      for (const cp of sortedPlacements) {
        drawPlacement(ctx, cp, atlases, atlasReady);
      }

      // Chairs (work seats) — derived from desk objects.
      for (const seat of scene.seats) {
        drawSeatChair(ctx, seat, atlases, atlasReady, catalog);
      }

      if (debug) {
        ctx.fillStyle = "rgba(255, 80, 80, 0.35)";
        for (let y = 0; y < scene.rows; y++) {
          for (let x = 0; x < scene.cols; x++) {
            if (!scene.walkable[y][x])
              ctx.fillRect(x * TILE, y * TILE, TILE, TILE);
          }
        }
        ctx.fillStyle = "rgba(80, 220, 120, 0.55)";
        for (const seat of scene.seats) {
          ctx.fillRect(seat.x * TILE + 2, seat.y * TILE + 2, TILE - 4, TILE - 4);
        }
      }

      // Sprites — y-sorted with placements would be ideal but agents move,
      // so just sort agents among themselves and draw on top.
      const sortedSprites = [...spritesRef.current.values()].sort(
        (a, b) => a.y - b.y,
      );
      for (const s of sortedSprites) {
        const frame = s.isMoving ? Math.floor(s.animTime * 8) : 0;
        drawCharacter(
          s.agent.spriteIdx % CHAR_URLS.length,
          s.dir,
          frame,
          s.x * TILE,
          s.y * TILE,
        );

        ctx.fillStyle = s.agent.busy ? "#3fb950" : "#8b949e";
        ctx.beginPath();
        ctx.arc(s.x * TILE + TILE / 2, s.y * TILE - TILE - 2, 1.4, 0, Math.PI * 2);
        ctx.fill();
      }

      // Overhead "sitting in" pass — only when the seat faces UP. In
      // that orientation the agent's back is to the camera, so the
      // chair/sofa back logically sits in front of them and should
      // re-render over the agent's body. Any other facing (down/left/
      // right) keeps the agent on top so their face/profile is visible.
      for (const s of sortedSprites) {
        if (s.state === "working" && s.seatIdx != null) {
          const seat = scene.seats[s.seatIdx];
          if (seat.dir === DIR_UP) {
            // Cover the agent with whatever furniture sits in their seat
            // cell: the auto-drawn chair from catalog.chair (if any), plus
            // any non-floor placement the user dropped at that cell (e.g.
            // a custom "Among Us" chair placed as a regular catalog object
            // — those don't go through drawSeatChair).
            drawSeatChair(ctx, seat, atlases, atlasReady, catalog);
            for (const cp of scene.placements) {
              if (cp.obj.kind === "floor") continue;
              if (seat.x < cp.x || seat.x >= cp.x + cp.w) continue;
              if (seat.y < cp.y || seat.y >= cp.y + cp.h) continue;
              drawPlacementCell(
                ctx,
                cp,
                seat.x - cp.x,
                seat.y - cp.y,
                atlases,
                atlasReady,
              );
            }
          }
          continue;
        }
        if (s.state === "resting") {
          const cx = Math.round(s.x);
          const cy = Math.round(s.y);
          for (const cp of scene.placements) {
            const rs = cp.obj.restSeat;
            if (!rs || rs.dir !== "up") continue;
            if (cx < cp.x || cx >= cp.x + cp.w) continue;
            if (cy < cp.y || cy >= cp.y + cp.h) continue;
            drawPlacementCell(
              ctx,
              cp,
              cx - cp.x,
              cy - cp.y,
              atlases,
              atlasReady,
            );
          }
        }
      }

      ctx.restore();

      ctx.textAlign = "center";
      for (const s of sortedSprites) {
        const screenX = (s.x + 0.5) * TILE * zoom;
        const headTop = (s.y - 1) * TILE * zoom;

        // Status badge above the name label — communicates what the
        // agent is currently doing. Compact one-word label keyed off
        // the FSM state.
        const status = statusLabel(s.state);
        ctx.font = "9px ui-monospace, monospace";
        const sw = ctx.measureText(status).width + 6;
        ctx.fillStyle = "rgba(0, 0, 0, 0.65)";
        ctx.fillRect(screenX - sw / 2, headTop - 26, sw, 10);
        ctx.fillStyle = statusColor(s.state);
        ctx.fillText(status, screenX, headTop - 18);

        // Name label.
        ctx.font = "10px ui-monospace, monospace";
        const label = s.agent.name;
        const w = ctx.measureText(label).width + 8;
        ctx.fillStyle = "rgba(0, 0, 0, 0.6)";
        ctx.fillRect(screenX - w / 2, headTop - 14, w, 12);
        ctx.fillStyle = s.agent.role === "main" ? "#e6edf3" : "#aff5b4";
        ctx.fillText(label, screenX, headTop - 5);
      }

      rafId = requestAnimationFrame(tick);
    };

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [open, zoom, debug, scene, BG_W, BG_H, editing, catalog, atlasReady, charReady]);

  const cw = BG_W * zoom;
  const ch = BG_H * zoom;

  return (
    <aside className={`office-panel${open ? " open" : ""}`} aria-hidden={!open}>
      <header className="office-panel-header">
        <div className="office-panel-title">
          <Users size={ICON} />
          <span>Office</span>
        </div>
        <div className="office-panel-stats">
          <span>{agents.length} agent{agents.length === 1 ? "" : "s"}</span>
          {subAgents.size > 0 && (
            <span className="office-stat-sub">
              · {subAgents.size} tool{subAgents.size === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <button
          type="button"
          className={`icon-button${editing ? " active" : ""}`}
          onClick={() => setEditing((e) => !e)}
          title="Toggle layout editor"
          aria-label="Toggle editor"
        >
          <Pencil size={ICON} />
        </button>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          title="Close"
          aria-label="Close office panel"
        >
          <X size={ICON} />
        </button>
      </header>
      <div className="office-panel-body">
        {editing ? (
          <OfficeEditor
            layout={layout}
            setLayout={setLayout}
            customCatalog={customCatalog}
            setCustomCatalog={setCustomCatalog}
            catalog={catalog}
            onPersist={persistState}
            zoom={zoom}
            showToast={showToast}
          />
        ) : (
          <div
            className="office-scene-wrap"
            style={{ aspectRatio: `${cw} / ${ch}` }}
          >
            <canvas
              ref={canvasRef}
              width={cw}
              height={ch}
              style={{
                width: "100%",
                height: "100%",
                display: "block",
                imageRendering: "pixelated",
              }}
            />
            {!assetsReady && (
              <div className="office-loader">
                <div className="office-spinner" />
                <span>Loading office…</span>
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
