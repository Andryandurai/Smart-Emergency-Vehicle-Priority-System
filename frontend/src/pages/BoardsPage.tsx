/** Digital road display board wall - what each roadside sign is showing. */
import { useState } from "react";

import { alerts } from "@/api/endpoints";
import type { DisplayBoardLive } from "@/api/types";
import { Empty, fmtTime } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { useSocket } from "@/hooks/useSocket";

interface BoardUpdate {
  board: string;
  message: string;
  expires_at: string;
  latitude: number;
  longitude: number;
}

export function BoardsPage() {
  const [boards, setBoards] = useState<Record<string, DisplayBoardLive>>({});

  usePolling(async (signal) => {
    const rows = await alerts.boards(signal);
    setBoards(Object.fromEntries(rows.map((board) => [board.code, board])));
  }, 8000);

  useSocket("/ws/ops/", {
    handlers: {
      display_board: (data) => {
        const update = data as BoardUpdate;
        setBoards((prev) => {
          const existing = prev[update.board];
          const next: DisplayBoardLive = {
            code: update.board,
            name: existing?.name ?? "",
            latitude: existing?.latitude ?? update.latitude,
            longitude: existing?.longitude ?? update.longitude,
            message: update.message,
            expires_at: update.expires_at,
          };
          return { ...prev, [update.board]: next };
        });
      },
    },
  });

  const rows = Object.values(boards);

  return (
    <div className="page scroll">
      <div className="page-head">
        <h1>Digital road display boards</h1>
        <p>
          Layer 4 pushes advance warnings to roadside variable-message signs and smart-city
          displays. This wall mirrors exactly what each sign is showing right now.
        </p>
      </div>

      <div className="grid-2">
        {rows.length === 0 && (
          <Empty>
            No display boards registered. Run <code>manage.py seed_demo</code>.
          </Empty>
        )}
        {rows.map((board) => (
          <div className="card" key={board.code}>
            <h3>
              {board.code}
              {board.name ? ` · ${board.name}` : ""}
            </h3>
            <div className={`board${board.message ? "" : " blank"}`}>
              {board.message || "- NO ACTIVE MESSAGE -"}
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
              {board.expires_at ? `expires ${fmtTime(board.expires_at)}` : "idle"} &middot;{" "}
              {board.latitude.toFixed(4)}, {board.longitude.toFixed(4)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
