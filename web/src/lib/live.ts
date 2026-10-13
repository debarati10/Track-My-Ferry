import type { LiveMessage } from "../types";
import { liveSocketUrl } from "./api";

export type ConnectionStatus = "connecting" | "live" | "reconnecting";

/** WebSocket client with exponential-backoff reconnects. */
export class LiveConnection {
  private ws: WebSocket | null = null;
  private retry = 0;
  private timer: number | undefined;
  private closed = false;

  constructor(
    private onMessage: (msg: LiveMessage) => void,
    private onStatus: (status: ConnectionStatus) => void,
  ) {}

  connect(): void {
    this.closed = false;
    this.onStatus(this.retry === 0 ? "connecting" : "reconnecting");
    const ws = new WebSocket(liveSocketUrl());
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.onStatus("live");
    };
    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        this.onMessage(JSON.parse(ev.data) as LiveMessage);
      } catch (err) {
        console.warn("bad live message", err);
      }
    };
    ws.onclose = () => {
      if (this.closed) return;
      this.onStatus("reconnecting");
      const delay = Math.min(30_000, 1000 * 2 ** this.retry++);
      this.timer = window.setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  close(): void {
    this.closed = true;
    window.clearTimeout(this.timer);
    this.ws?.close();
  }
}
