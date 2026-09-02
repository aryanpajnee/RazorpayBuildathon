import { LANES, type LaneId, type LaneState } from "../reducer";

const LANE_HINT: Record<LaneId, string> = {
  intent: "reads the request, grants bounded authority",
  brain: "the tool-calling loop — reasons, decides",
  search: "finds real candidates on the open web",
  merchant: "re-lists the pick, issues the signed quote",
  gate: "re-derives the total, enforces the mandate",
  ledger: "hash-chains every decision, tamper-evident",
};

interface Props {
  states: Record<LaneId, LaneState>;
}

export default function AgentRoster({ states }: Props) {
  return (
    <section className="panel roster" aria-label="Agent roster">
      <h2 className="panel__title">Agent roster</h2>
      <ul className="roster__list">
        {LANES.map(({ id, label }) => {
          const state = states[id];
          return (
            <li key={id} className={`roster__row roster__row--${state}`}>
              <span className="roster__dot" aria-hidden />
              <div className="roster__copy">
                <span className="roster__label">{label}</span>
                <span className="roster__hint">{LANE_HINT[id]}</span>
              </div>
              <span className="roster__state">
                {state === "active" ? "active" : state === "settled" ? "done" : "idle"}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
