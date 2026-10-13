/**
 * Top-down ferry icons, drawn once per (route color, moving/docked) on a canvas and handed to
 * MapLibre as images. The bow points north; the map rotates each icon to the boat's course.
 * Image ids look like `ship:#F5A623:m` so a layer can pick one with a plain expression and the
 * map's `styleimagemissing` hook can draw any color on demand.
 */
export const SHIP_PREFIX = "ship:";
export const SHIP_PIXEL_RATIO = 3;

const LENGTH = 30; // css px at icon-size 1
const BEAM = 12;
const WAKE = 24;
const WIDTH = 36;
const HEIGHT = 2 * (LENGTH / 2 + WAKE); // symmetric so the boat's center is the image center

function rgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.padEnd(6, "0").slice(0, 6);
  const n = parseInt(full, 16);
  return Number.isNaN(n) ? [140, 150, 160] : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function shade(hex: string, f: number): string {
  const [r, g, b] = rgb(hex);
  const mix = (c: number) => Math.round(f >= 0 ? c + (255 - c) * f : c * (1 + f));
  return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

/** Hull outline: pointed bow, straight sides, square stern with rounded corners. */
function hullPath(ctx: CanvasRenderingContext2D, cx: number, top: number) {
  const half = BEAM / 2;
  const bottom = top + LENGTH;
  ctx.beginPath();
  ctx.moveTo(cx, top);
  ctx.bezierCurveTo(cx + half * 0.6, top + 1.5, cx + half, top + LENGTH * 0.2, cx + half, top + LENGTH * 0.4);
  ctx.lineTo(cx + half, bottom - 2.5);
  ctx.quadraticCurveTo(cx + half, bottom, cx + half - 2.5, bottom);
  ctx.lineTo(cx - half + 2.5, bottom);
  ctx.quadraticCurveTo(cx - half, bottom, cx - half, bottom - 2.5);
  ctx.lineTo(cx - half, top + LENGTH * 0.4);
  ctx.bezierCurveTo(cx - half, top + LENGTH * 0.2, cx - half * 0.6, top + 1.5, cx, top);
  ctx.closePath();
}

function drawWake(ctx: CanvasRenderingContext2D, cx: number, stern: number) {
  const fade = (alpha: number) => {
    const g = ctx.createLinearGradient(0, stern - 2, 0, stern + WAKE);
    g.addColorStop(0, `rgba(255, 255, 255, ${alpha})`);
    g.addColorStop(1, "rgba(255, 255, 255, 0)");
    return g;
  };
  ctx.lineCap = "round";
  // propeller wash straight behind the stern
  ctx.strokeStyle = fade(0.75);
  ctx.lineWidth = 5;
  ctx.beginPath();
  ctx.moveTo(cx, stern - 1);
  ctx.lineTo(cx, stern + WAKE * 0.8);
  ctx.stroke();
  // the V-shaped bow waves trailing off each side
  ctx.strokeStyle = fade(0.95);
  ctx.lineWidth = 1.6;
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(cx + side * (BEAM / 2 - 0.5), stern - 4);
    ctx.quadraticCurveTo(cx + side * (BEAM / 2 + 1.5), stern + WAKE * 0.45, cx + side * (BEAM / 2 + 6.5), stern + WAKE);
    ctx.stroke();
  }
}

export function drawShip(color: string, moving: boolean): ImageData {
  const R = SHIP_PIXEL_RATIO;
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH * R;
  canvas.height = HEIGHT * R;
  const ctx = canvas.getContext("2d")!;
  ctx.scale(R, R);
  const cx = WIDTH / 2;
  const top = HEIGHT / 2 - LENGTH / 2;
  const stern = top + LENGTH;

  if (moving) drawWake(ctx, cx, stern);

  // white silhouette slightly larger than the hull, with a soft drop shadow (shadow units are device px)
  ctx.save();
  ctx.shadowColor = "rgba(12, 28, 44, 0.45)";
  ctx.shadowBlur = 3 * R;
  ctx.shadowOffsetY = 1.2 * R;
  hullPath(ctx, cx, top);
  ctx.fillStyle = "#ffffff";
  ctx.fill();
  ctx.lineJoin = "round";
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#ffffff";
  ctx.stroke();
  ctx.restore();

  // painted hull with a darker rim
  hullPath(ctx, cx, top);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.lineWidth = 0.9;
  ctx.strokeStyle = shade(color, -0.35);
  ctx.stroke();

  // superstructure: white cabin, dark windshield, colored upper deck
  const cabinTop = top + LENGTH * 0.3;
  const cabinH = LENGTH * 0.52;
  roundRect(ctx, cx - 3.9, cabinTop, 7.8, cabinH, 2);
  ctx.fillStyle = "rgba(255, 255, 255, 0.97)";
  ctx.fill();
  roundRect(ctx, cx - 3.3, cabinTop + 0.8, 6.6, 2.2, 1);
  ctx.fillStyle = "#1c2833";
  ctx.fill();
  roundRect(ctx, cx - 2.5, cabinTop + 4.4, 5, cabinH - 6.4, 1.4);
  ctx.fillStyle = shade(color, 0.12);
  ctx.fill();
  // bow deck stripe
  ctx.beginPath();
  ctx.moveTo(cx, top + 3.5);
  ctx.lineTo(cx, cabinTop - 2);
  ctx.lineWidth = 1;
  ctx.strokeStyle = "rgba(255, 255, 255, 0.7)";
  ctx.lineCap = "round";
  ctx.stroke();

  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

/** Parse `ship:<color>:<m|d>` back into what to draw, or null for other ids. */
export function parseShipId(id: string): { color: string; moving: boolean } | null {
  if (!id.startsWith(SHIP_PREFIX)) return null;
  const [, color, state] = id.split(":");
  if (!color) return null;
  return { color, moving: state !== "d" };
}
