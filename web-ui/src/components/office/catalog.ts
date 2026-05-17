// Tile-object catalog and office layout for the Office panel.
//
// The catalog describes each piece of furniture/floor/wall as a typed
// object: its sprite source rect (in one of the LimeZu atlases), its
// footprint in tile units, whether it blocks movement, and — for
// workstations — where the agent sits and which way they face.
//
// The layout names a grid size and places catalog entries on it. The
// engine derives the walkability mask and the list of work seats from
// these structures, so what's drawn always matches what's collidable.

export type AtlasId = "room" | "furniture";

export interface AtlasSprite {
  atlas: AtlasId;
  // Source pixel coords inside the atlas. Width/height default to the
  // object's footprint × TILE_SIZE (i.e. 16 px per tile).
  sx: number;
  sy: number;
  sw?: number;
  sh?: number;
}

export type ObjectKind =
  | "floor"
  | "wall"
  | "desk"
  | "chair"
  | "plant"
  | "decor";

export type SeatDir = "up" | "down" | "left" | "right";

export interface TileObject {
  id: string;
  // Human-readable label shown in the editor's catalog. Falls back to id
  // when absent (older entries created before this field existed).
  title?: string;
  kind: ObjectKind;
  // Footprint in tile units. Defaults to 1×1.
  w?: number;
  h?: number;
  // Sprite from one of the LimeZu atlases. If absent, the engine falls
  // back to a kind-appropriate solid color so iteration on tile coords
  // doesn't block layout work.
  sprite?: AtlasSprite;
  fallbackColor?: string;
  // Whether the object's footprint blocks movement. Defaults follow the
  // kind (walls/desks/plants/decor block; floors/chairs are walkable).
  blocks?: boolean;
  // Per-cell override of `blocks`. Shape must match `h × w` (rows × cols).
  // `true` blocks that cell, `false` lets agents walk through it. Lets you
  // model a desk whose top half is empty pixel art — bbox is 2×2, but only
  // the bottom row actually obstructs. When absent and `blocks` is true,
  // every cell in the footprint blocks.
  collisionMask?: boolean[][];
  // Tombstone marker — when set on a custom entry that shadows a built-in
  // by id, the merged catalog drops the entry entirely. Lets the editor
  // "delete" built-ins without modifying source. Removing the tombstone
  // (custom entry) restores the built-in.
  hidden?: boolean;
  // Render order. Higher z draws on top of lower; ties are broken by row
  // (back-to-front). When omitted, the engine derives a z from `kind` via
  // `defaultZ()` so most objects "just work" without per-tile tuning.
  z?: number;
  // Optional pixel-precise visual offset applied to the drawn sprite (not
  // collision). Handy when a sprite extends slightly past its 16-pixel
  // cell — e.g. shifting a tall chair up by 4px so the back overlaps the
  // desk behind it. Negative values move up/left.
  offsetX?: number;
  offsetY?: number;
  // Workstation seat metadata. The seat cell is offset from the object's
  // top-left corner; the agent stands there and faces `dir`. Setting
  // `primary: true` marks this as the preferred desk for the main agent
  // (one per office is usually enough — extras just become regular seats
  // when more than one main agent ends up using the layout).
  workSeat?: { dx: number; dy: number; dir: SeatDir; primary?: boolean };
  // Rest spot metadata — same shape as workSeat, but idle agents head
  // here to relax for a few seconds when they have no busy task to do.
  // Use this on sofas, beds, lounge chairs.
  restSeat?: { dx: number; dy: number; dir: SeatDir };
}

export function defaultZ(kind: ObjectKind): number {
  switch (kind) {
    case "floor":
      return 0;
    case "wall":
      return 1;
    case "desk":
    case "plant":
    case "decor":
      return 2;
    case "chair":
      return 3;
    default:
      return 1;
  }
}

// Defaults for blocking based on kind. Chair tiles stay walkable so an
// agent can stand on them; everything else with a footprint blocks.
export function defaultBlocks(kind: ObjectKind): boolean {
  switch (kind) {
    case "floor":
    case "chair":
      return false;
    default:
      return true;
  }
}

// Catalog of placeable objects.
//
// Sprite coords below are tuned against the LimeZu Modern Office (Revamped)
// sheets. Refine them in place when something looks off — the engine reads
// from this table on every render, no rebuild needed beyond the usual HMR.
//
// To map a tile-grid coord (col, row) to sprite pixel coords: multiply by 16.
// `Room_Builder_Office_16x16.png` is 16 cols × 14 rows.
// `Modern_Office_Shadowless_16x16.png` is 16 cols × 53 rows.
export const CATALOG: Record<string, TileObject> = {
  // Floor — plain gray office tile from Room_Builder_Office.
  floor: {
    id: "floor",
    kind: "floor",
    sprite: { atlas: "room", sx: 192, sy: 80 }, // (col 12, row 5)
    fallbackColor: "#d3d6dc",
  },

  // Wall — plain wall edge from Room_Builder_Office's top section.
  wall_h: {
    id: "wall_h",
    kind: "wall",
    sprite: { atlas: "room", sx: 64, sy: 16 }, // (col 4, row 1)
    fallbackColor: "#5e6470",
  },
  wall_v: {
    id: "wall_v",
    kind: "wall",
    sprite: { atlas: "room", sx: 16, sy: 32 }, // (col 1, row 2)
    fallbackColor: "#5e6470",
  },

  // Desk with computer — 1-tile sprite. Coord placeholder; visible
  // confirmation needed. Falls back to a brown rectangle until tuned.
  desk_pc_down: {
    id: "desk_pc_down",
    kind: "desk",
    fallbackColor: "#caa17a",
    workSeat: { dx: 0, dy: 1, dir: "up" },
  },
  desk_pc_up: {
    id: "desk_pc_up",
    kind: "desk",
    fallbackColor: "#caa17a",
    workSeat: { dx: 0, dy: -1, dir: "down" },
  },

  // Chair sprite — coord placeholder. Falls back to a dark ellipse.
  chair: {
    id: "chair",
    kind: "chair",
    fallbackColor: "#3a3e47",
  },

  // Plant — 1×1 decoration that blocks movement.
  plant: {
    id: "plant",
    kind: "plant",
    fallbackColor: "#3fb950",
  },
};

// User-defined catalog entries — created in the editor by drag-selecting
// a region of an atlas. Persisted via the backend (project file
// `web-ui/public/office/state.json`) so changes survive reloads and can
// be committed alongside the code. At runtime the built-in CATALOG and
// the custom map are merged; custom entries shadow built-ins on id
// collision.
export type CatalogMap = Record<string, TileObject>;

export function mergeCatalog(custom: CatalogMap): CatalogMap {
  const merged: CatalogMap = { ...CATALOG };
  for (const [id, obj] of Object.entries(custom)) {
    if (obj.hidden) {
      delete merged[id];
    } else {
      merged[id] = obj;
    }
  }
  return merged;
}

export interface Placement {
  // The id can name either a built-in or a user-defined catalog entry.
  id: string;
  x: number;
  y: number;
}

export interface OfficeLayout {
  cols: number;
  rows: number;
  // Tiles to draw + collide. Floor and outer walls are implicit — the
  // engine paints floor on every walkable cell and walls on the outer
  // ring, so layouts only list furniture.
  placements: Placement[];
}

// Open-space office: 4 desks top + 4 desks bottom, plants in corners.
// Chairs are derived from each desk's `workSeat` and rendered automatically.
export const DEFAULT_LAYOUT: OfficeLayout = {
  cols: 17,
  rows: 12,
  placements: [
    // Top desk row — desks face DOWN (chair below).
    { id: "desk_pc_down", x: 2, y: 3 },
    { id: "desk_pc_down", x: 6, y: 3 },
    { id: "desk_pc_down", x: 10, y: 3 },
    { id: "desk_pc_down", x: 14, y: 3 },
    // Bottom desk row — desks face UP (chair above).
    { id: "desk_pc_up", x: 2, y: 9 },
    { id: "desk_pc_up", x: 6, y: 9 },
    { id: "desk_pc_up", x: 10, y: 9 },
    { id: "desk_pc_up", x: 14, y: 9 },

    // Plants in the four corners.
    { id: "plant", x: 0, y: 1 },
    { id: "plant", x: 15, y: 1 },
    { id: "plant", x: 0, y: 11 },
    { id: "plant", x: 15, y: 11 },
  ],
};
