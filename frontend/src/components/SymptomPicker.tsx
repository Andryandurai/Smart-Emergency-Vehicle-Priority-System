/**
 * What the crew can actually see.
 *
 * The category picker asks for a diagnosis. At a roadside, with an
 * unresponsive patient and no history, a crew frequently cannot give one -
 * and forcing a guess between "cardiac" and "stroke" produces a confident
 * wrong answer that routes the patient to a hospital which cannot treat them.
 *
 * So this exists alongside the category, not instead of it. Every choice
 * shows what it will do to the hospital search before it is made, because a
 * crew should be able to see that ticking "Paralysis" sends the patient to a
 * stroke unit.
 */
import type { SymptomAssessment, SymptomCode, SymptomSpec } from "@/api/types";
import { Badge, levelClass } from "@/components/ui";

export function SymptomPicker({
  catalogue,
  selected,
  onToggle,
  assessment,
  required,
}: {
  catalogue: SymptomSpec[];
  selected: SymptomCode[];
  onToggle: (code: SymptomCode) => void;
  /** Echoed back by the recommender once a search has run. */
  assessment?: SymptomAssessment | undefined;
  /** True when the category is undetermined, so symptoms are the only input. */
  required: boolean;
}) {
  const chosen = new Set(selected);

  return (
    <div className="symptom-block">
      <div className="symptom-head">
        <span>
          What can you see?{" "}
          {required ? (
            <Badge tone="bad">required</Badge>
          ) : (
            <span className="muted small">optional, but helps the hospital prepare</span>
          )}
        </span>
        {selected.length > 0 && (
          <span className="muted small">{selected.length} selected</span>
        )}
      </div>

      {required && selected.length === 0 && (
        <p className="hint">
          You chose <b>Not sure / Other</b>. Tick what you can observe — that is what the
          hospital match will be based on.
        </p>
      )}

      <div className="symptom-grid">
        {catalogue.map((symptom) => {
          const on = chosen.has(symptom.code);
          return (
            <button
              key={symptom.code}
              type="button"
              className={`symptom${on ? " selected" : ""}`}
              data-level={symptom.priority_level}
              onClick={() => onToggle(symptom.code)}
              title={symptom.note}
            >
              <span className="sym-label">{symptom.label}</span>
              <span className={`sym-level ${levelClass(symptom.priority_level)}`}>
                L{symptom.priority_level}
              </span>
            </button>
          );
        })}
      </div>

      {assessment && assessment.symptoms.length > 0 && (
        <div className="symptom-readout">
          <div className="row">
            <span className="k">Response level</span>
            <b className={levelClass(assessment.priority_level)}>
              Level {assessment.priority_level}
            </b>
          </div>
          {assessment.required_facilities.length > 0 && (
            <div className="row">
              <span className="k">Hospital must have</span>
              <b>
                {assessment.required_facilities
                  .map((facility) => facility.replaceAll("_", " "))
                  .join(", ")}
              </b>
            </div>
          )}
          {assessment.notes.map((note) => (
            <p key={note} className="sym-note">
              {note}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
