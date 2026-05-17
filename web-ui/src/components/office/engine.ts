// Office engine: turns a (catalog, layout) pair into a renderable scene
// with walkability and work-seat metadata. The catalog and layout live in
// `./catalog.ts`; this module only consumes them.

import {
  CATALOG,
  OfficeLayout,
  Placement,
  SeatDir,
  TileObject,
  defaultBlocks,
} from "./catalog";

export type CatalogMap = Record<string, TileObject>;

export const TILE = 16;

export type Dir = 0 | 1 | 2 | 3;
// LimeZu character sheets pack 24 frames in one row as 6 frames × 4
// directions, ordered RIGHT, UP, LEFT, DOWN. Aligning these constants
// with the file's frame blocks means `(dir * 6 + frame) * 16` lands on
// the correct sprite without any extra lookup table.
export const DIR_RIGHT: Dir = 0;
export const DIR_UP: Dir = 1;
export const DIR_LEFT: Dir = 2;
export const DIR_DOWN: Dir = 3;

const DIR_MAP: Record<SeatDir, Dir> = {
  down: DIR_DOWN,
  up: DIR_UP,
  right: DIR_RIGHT,
  left: DIR_LEFT,
};

export interface CompiledSeat {
  x: number;
  y: number;
  dir: Dir;
  primary?: boolean;
}

export interface CompiledPlacement extends Placement {
  obj: TileObject;
  w: number;
  h: number;
}

export interface CompiledScene {
  cols: number;
  rows: number;
  walkable: boolean[][];
  placements: CompiledPlacement[];
  // Workstations — agents head here when their session is busy.
  seats: CompiledSeat[];
  // Rest spots (sofas, lounge chairs) — idle agents pick these up
  // randomly while wandering.
  restSeats: CompiledSeat[];
}

// Compile a layout into a renderable scene. The walkability mask blocks
// outer walls + every cell covered by a non-walkable object's footprint;
// seats are emitted in placement order so `seatIdx` is stable across
// re-compiles (relevant when the panel hot-reloads).
export function compileLayout(
  layout: OfficeLayout,
  catalog: CatalogMap = CATALOG,
): CompiledScene {
  const { cols, rows, placements } = layout;
  // Everything starts walkable. Collision is driven purely by placements
  // so the editor's "what you see is what blocks" rule holds — no implicit
  // outer-ring walls baked into the engine.
  const walkable: boolean[][] = [];
  for (let y = 0; y < rows; y++) {
    const row: boolean[] = [];
    for (let x = 0; x < cols; x++) row.push(true);
    walkable.push(row);
  }

  const compiledPlacements: CompiledPlacement[] = [];
  const seats: CompiledSeat[] = [];
  const restSeats: CompiledSeat[] = [];

  for (const p of placements) {
    const obj = catalog[p.id];
    if (!obj) {
      // eslint-disable-next-line no-console
      console.warn(`office: unknown placement id "${p.id}"`);
      continue;
    }
    const w = obj.w ?? 1;
    const h = obj.h ?? 1;
    const blocks = obj.blocks ?? defaultBlocks(obj.kind);
    if (blocks) {
      const mask = obj.collisionMask;
      for (let dy = 0; dy < h; dy++) {
        for (let dx = 0; dx < w; dx++) {
          const cellBlocks = mask ? mask[dy]?.[dx] ?? true : true;
          if (!cellBlocks) continue;
          const x = p.x + dx;
          const y = p.y + dy;
          if (x >= 0 && y >= 0 && x < cols && y < rows) {
            walkable[y][x] = false;
          }
        }
      }
    }
    if (obj.workSeat) {
      const seatX = p.x + obj.workSeat.dx;
      const seatY = p.y + obj.workSeat.dy;
      seats.push({
        x: seatX,
        y: seatY,
        dir: DIR_MAP[obj.workSeat.dir],
        primary: obj.workSeat.primary || undefined,
      });
    }
    if (obj.restSeat) {
      const seatX = p.x + obj.restSeat.dx;
      const seatY = p.y + obj.restSeat.dy;
      restSeats.push({ x: seatX, y: seatY, dir: DIR_MAP[obj.restSeat.dir] });
    }
    compiledPlacements.push({ ...p, obj, w, h });
  }

  return { cols, rows, walkable, placements: compiledPlacements, seats, restSeats };
}

// BFS pathfinder. Tiny grids, so a queue+map outperforms anything fancier.
export function findPath(
  scene: CompiledScene,
  sx: number,
  sy: number,
  gx: number,
  gy: number,
): Array<[number, number]> {
  if (sx === gx && sy === gy) return [];
  const { cols, rows, walkable } = scene;
  const key = (x: number, y: number) => y * cols + x;
  const prev = new Map<number, number>();
  const visited = new Set<number>([key(sx, sy)]);
  const queue: Array<[number, number]> = [[sx, sy]];
  const NEIGHBORS: Array<[number, number]> = [
    [1, 0],
    [-1, 0],
    [0, 1],
    [0, -1],
  ];
  let found = false;
  while (queue.length > 0) {
    const [x, y] = queue.shift()!;
    if (x === gx && y === gy) {
      found = true;
      break;
    }
    for (const [dx, dy] of NEIGHBORS) {
      const nx = x + dx;
      const ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
      // The goal cell may be a non-walkable seat-on-chair; allow stepping
      // onto the final target even if it's nominally blocked. (Chairs are
      // already walkable in our default catalog, so this is just a safety
      // net.)
      if (!walkable[ny][nx] && !(nx === gx && ny === gy)) continue;
      const nk = key(nx, ny);
      if (visited.has(nk)) continue;
      visited.add(nk);
      prev.set(nk, key(x, y));
      queue.push([nx, ny]);
    }
  }
  if (!found) return [];
  const path: Array<[number, number]> = [];
  let cur = key(gx, gy);
  while (cur !== key(sx, sy)) {
    const x = cur % cols;
    const y = Math.floor(cur / cols);
    path.unshift([x, y]);
    const p = prev.get(cur);
    if (p === undefined) break;
    cur = p;
  }
  return path;
}

export function pickWanderTarget(scene: CompiledScene): [number, number] {
  // Random first — cheap on mostly-empty grids.
  for (let i = 0; i < 40; i++) {
    const x = Math.floor(Math.random() * scene.cols);
    const y = Math.floor(Math.random() * scene.rows);
    if (scene.walkable[y][x]) return [x, y];
  }
  // Crowded grid: deterministic scan for the first walkable cell so we
  // never hand back a blocked tile (which would leave an agent stuck).
  for (let y = 0; y < scene.rows; y++) {
    for (let x = 0; x < scene.cols; x++) {
      if (scene.walkable[y][x]) return [x, y];
    }
  }
  // Whole map is blocked. Caller has nothing useful to do here but the
  // return type stays consistent; runtime should also use the per-tick
  // escape logic for agents that started outside walkable space.
  return [0, 0];
}

// BFS outward from (sx, sy) for the closest walkable cell. Returns the
// start if it's already walkable, or null when every cell on the grid is
// blocked. Used to "rescue" an agent that ends up inside an object after
// a placement edit, and to validate spawn positions.
export function findNearestWalkable(
  scene: CompiledScene,
  sx: number,
  sy: number,
): [number, number] | null {
  if (
    sx >= 0 &&
    sy >= 0 &&
    sx < scene.cols &&
    sy < scene.rows &&
    scene.walkable[sy][sx]
  ) {
    return [sx, sy];
  }
  const key = (x: number, y: number) => y * scene.cols + x;
  const visited = new Set<number>([key(sx, sy)]);
  const queue: Array<[number, number]> = [[sx, sy]];
  const NEIGHBORS: Array<[number, number]> = [
    [1, 0],
    [-1, 0],
    [0, 1],
    [0, -1],
  ];
  while (queue.length > 0) {
    const [x, y] = queue.shift()!;
    for (const [dx, dy] of NEIGHBORS) {
      const nx = x + dx;
      const ny = y + dy;
      if (nx < 0 || ny < 0 || nx >= scene.cols || ny >= scene.rows) continue;
      const k = key(nx, ny);
      if (visited.has(k)) continue;
      visited.add(k);
      if (scene.walkable[ny][nx]) return [nx, ny];
      queue.push([nx, ny]);
    }
  }
  return null;
}

export function dirFromVec(dx: number, dy: number): Dir {
  if (Math.abs(dx) >= Math.abs(dy)) {
    return dx >= 0 ? DIR_RIGHT : DIR_LEFT;
  }
  return dy >= 0 ? DIR_DOWN : DIR_UP;
}

// Drawing helpers: render a placement either from its atlas sprite or
// from a kind-appropriate solid-color fallback. Centralized here so swap-
// ping to real sprites later is a single-call change.
export function drawPlacement(
  ctx: CanvasRenderingContext2D,
  cp: CompiledPlacement,
  atlases: Partial<Record<string, HTMLImageElement>>,
  atlasReady: Partial<Record<string, boolean>>,
) {
  // Collision is locked to the tile grid, but rendering can be shifted
  // pixel-by-pixel via offsetX/offsetY so a sprite can poke out of its
  // cell without changing where the engine thinks it sits.
  const ox = cp.obj.offsetX ?? 0;
  const oy = cp.obj.offsetY ?? 0;
  const px = cp.x * TILE + ox;
  const py = cp.y * TILE + oy;
  const w = cp.w * TILE;
  const h = cp.h * TILE;
  const sprite = cp.obj.sprite;
  if (sprite) {
    const atlas = atlases[sprite.atlas];
    if (atlas && atlasReady[sprite.atlas]) {
      const sw = sprite.sw ?? w;
      const sh = sprite.sh ?? h;
      ctx.drawImage(atlas, sprite.sx, sprite.sy, sw, sh, px, py, w, h);
      return;
    }
  }
  drawFallback(ctx, cp, px, py, w, h);
}

function drawFallback(
  ctx: CanvasRenderingContext2D,
  cp: CompiledPlacement,
  px: number,
  py: number,
  w: number,
  h: number,
) {
  const color = cp.obj.fallbackColor ?? "#888";
  switch (cp.obj.kind) {
    case "desk": {
      // Desk + monitor primitive.
      ctx.fillStyle = color;
      ctx.fillRect(px + 1, py + 2, w - 2, h - 3);
      ctx.fillStyle = "#dbb38b";
      ctx.fillRect(px + 1, py + 2, w - 2, 3);
      ctx.fillStyle = "#6e5037";
      ctx.fillRect(px + 1, py + h - 3, w - 2, 2);
      // Monitor placed away from the chair side so the screen "faces" it.
      const facesDown = cp.obj.workSeat?.dir === "up";
      const monitorY = facesDown ? py + 2 : py + h - 9;
      ctx.fillStyle = "#2c2f36";
      ctx.fillRect(px + 3, monitorY, w - 6, 7);
      ctx.fillStyle = "#1f6feb";
      ctx.fillRect(px + 4, monitorY + 1, w - 8, 5);
      return;
    }
    case "chair": {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.ellipse(
        px + w / 2,
        py + h / 2 + 1,
        w / 2 - 2,
        h / 2 - 2,
        0,
        0,
        Math.PI * 2,
      );
      ctx.fill();
      return;
    }
    case "plant": {
      ctx.fillStyle = "#7a4d2b";
      ctx.fillRect(px + 4, py + 9, w - 8, 6);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px + w / 2, py + 7, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(px + 4, py + 8, 3, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(px + w - 4, py + 8, 3, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    default:
      ctx.fillStyle = color;
      ctx.fillRect(px, py, w, h);
  }
}

// Draw just one tile of a (possibly multi-tile) placement. Used by the
// runtime "seated agent" overlay so a 2×2 sofa doesn't fully cover the
// agent — we redraw only the single seat cell over their body.
export function drawPlacementCell(
  ctx: CanvasRenderingContext2D,
  cp: CompiledPlacement,
  cellDx: number,
  cellDy: number,
  atlases: Partial<Record<string, HTMLImageElement>>,
  atlasReady: Partial<Record<string, boolean>>,
) {
  if (cellDx < 0 || cellDy < 0 || cellDx >= cp.w || cellDy >= cp.h) return;
  const ox = cp.obj.offsetX ?? 0;
  const oy = cp.obj.offsetY ?? 0;
  const px = (cp.x + cellDx) * TILE + ox;
  const py = (cp.y + cellDy) * TILE + oy;
  const sprite = cp.obj.sprite;
  if (!sprite) return;
  const atlas = atlases[sprite.atlas];
  if (!atlas || !atlasReady[sprite.atlas]) return;
  ctx.drawImage(
    atlas,
    sprite.sx + cellDx * TILE,
    sprite.sy + cellDy * TILE,
    TILE,
    TILE,
    px,
    py,
    TILE,
    TILE,
  );
}

// Render a "chair tile" at a work seat. Drawn separately because seats are
// derived from desk objects, not placed explicitly; they always use the
// catalog's "chair" object so the chair sprite/color stays in one place.
export function drawSeatChair(
  ctx: CanvasRenderingContext2D,
  seat: CompiledSeat,
  atlases: Partial<Record<string, HTMLImageElement>>,
  atlasReady: Partial<Record<string, boolean>>,
  catalog: CatalogMap = CATALOG,
) {
  const chair = catalog.chair;
  if (!chair) return;
  const fake: CompiledPlacement = {
    id: "chair" as keyof typeof CATALOG,
    x: seat.x,
    y: seat.y,
    obj: chair,
    w: chair.w ?? 1,
    h: chair.h ?? 1,
  };
  drawPlacement(ctx, fake, atlases, atlasReady);
}
