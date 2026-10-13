/**
 * Maps wall-clock time to the data's clock.
 *
 * Live data runs at 1x; the simulator demo can run faster, so the rate is estimated from
 * successive data timestamps instead of assumed. Used for dead reckoning and "in 6 min" labels.
 */
export class ClockSync {
  private anchorWall = 0;
  private anchorData = 0;
  private rate = 1;
  private lastWall = 0;
  private lastData = 0;
  private samples = 0;

  observe(dataTs: number, wallMs: number = performance.now()): void {
    const wall = wallMs / 1000;
    if (this.anchorWall === 0) {
      this.anchorWall = this.lastWall = wall;
      this.anchorData = this.lastData = dataTs;
      return;
    }
    if (dataTs <= this.lastData) return;
    const dw = wall - this.lastWall;
    if (dw > 2) {
      const r = (dataTs - this.lastData) / dw;
      if (Number.isFinite(r) && r > 0.05 && r < 200) {
        this.rate = this.samples++ === 0 ? r : 0.6 * this.rate + 0.4 * r;
      }
      this.lastWall = wall;
      this.lastData = dataTs;
    }
    // re-anchor so the estimate never drifts far from the newest observation
    const predicted = this.now(wallMs);
    if (Math.abs(predicted - dataTs) > 5 * Math.max(this.rate, 1)) {
      this.anchorWall = wall;
      this.anchorData = dataTs;
    } else if (dataTs > predicted) {
      this.anchorWall = wall;
      this.anchorData = predicted + 0.5 * (dataTs - predicted);
    }
  }

  now(wallMs: number = performance.now()): number {
    if (this.anchorWall === 0) return Date.now() / 1000;
    return this.anchorData + (wallMs / 1000 - this.anchorWall) * this.rate;
  }

  get speed(): number {
    return this.rate;
  }
}
