import { useEffect, useRef } from "react";

export interface LiveDetectionEvent {
  node_id: string;
  participant_id: string | null;
  name: string | null;
  activity_id: string | null;
  checkin: "new" | "already";
  at: string;
}

function wsBase(): string {
  return (window.location.protocol === "https:" ? "wss://" : "ws://") + window.location.host;
}

const RECONNECT_DELAY_MS = 3000;

/**
 * Subscribes to the app-wide live recognition feed — the kiosk
 * (api/recognition.py), every CCTV camera node, and mobile central-inference
 * all publish here (see backend/app/services/events_feed.py) — and calls
 * `onEvent` for each one for as long as this component is mounted.
 *
 * This is what lets People / PersonDetail / Attendees / Dashboard update
 * themselves the moment a detection happens, instead of only on a manual
 * page reload. Reconnects automatically after a drop so a long-lived admin
 * tab does not silently go stale.
 */
export function useLiveEvents(onEvent: (event: LiveDetectionEvent) => void): void {
  // A ref, not a dependency — so callers can pass a fresh inline arrow every
  // render without tearing down and reopening the socket each time.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;

    function connect() {
      if (cancelled) return;
      const token = localStorage.getItem("token") ?? "";
      ws = new WebSocket(`${wsBase()}/api/node/events-feed?token=${encodeURIComponent(token)}`);
      ws.onmessage = (ev) => {
        try {
          onEventRef.current(JSON.parse(ev.data));
        } catch {
          /* malformed event — ignore rather than break the page over it */
        }
      };
      ws.onclose = () => {
        if (!cancelled) reconnectTimer = window.setTimeout(connect, RECONNECT_DELAY_MS);
      };
      ws.onerror = () => ws?.close(); // onclose above owns the actual reconnect
    }
    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
