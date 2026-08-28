import { useEffect, useRef } from "react";
import type { ConversationLine } from "../App";

const ROLE_LABEL: Record<ConversationLine["role"], string> = {
  buyer: "AI BUYER",
  merchant: "MERCHANT",
  gate: "GATE",
  system: "SYSTEM",
};

export default function ConversationPanel(props: {
  lines: ConversationLine[];
  actN: number | null;
  actTitle: string | null;
  status: "idle" | "running" | "done";
}) {
  const { lines, actN, actTitle, status } = props;
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines.length]);

  return (
    <section className="panel panel-convo">
      <div className="panel-head">
        <span className="panel-eyebrow">01</span>
        <h2>Agent conversation</h2>
      </div>

      {actN && (
        <div className="act-banner">
          <span className="act-n">ACT {actN}</span>
          <span className="act-title">{actTitle}</span>
        </div>
      )}

      <div className="convo-scroll">
        {lines.length === 0 && status === "idle" && (
          <p className="empty">Press <b>run demo</b>. An AI buyer will transact against the merchant, and every decision the Gate makes will play out here.</p>
        )}
        {lines.map((line, i) => (
          <div key={i} className={`bubble bubble-${line.role}`}>
            <div className="bubble-role">{ROLE_LABEL[line.role]}</div>
            <div className="bubble-text">{line.text}</div>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </section>
  );
}
