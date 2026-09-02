import { useEffect, useRef } from "react";
import type { AppEvent } from "../types";
import TimelineCard from "./TimelineCard";

interface Props {
  events: AppEvent[];
}

export default function Timeline({ events }: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [events.length]);

  return (
    <section className="panel timeline" aria-label="Live timeline">
      <h2 className="panel__title">Live timeline</h2>
      <div className="timeline__scroll" ref={scrollRef}>
        {events.length === 0 ? (
          <p className="timeline__empty">Nothing yet — authorize a run to watch the buyer work.</p>
        ) : (
          <div className="timeline__list">
            {events.map((event, i) => (
              <div className="timeline__item" key={event.seq}>
                <TimelineCard event={event} index={i} />
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
