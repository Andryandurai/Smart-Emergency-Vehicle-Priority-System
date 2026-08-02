# SEVPS AI & Computer Vision (Phases 6–7)

## The contract every prediction obeys

```json
{
  "prediction": 0.815,
  "confidence": 0.694,
  "is_actionable": true,
  "source": "model",
  "explanation": {
    "method": "shap",
    "baseline": 0.8828,
    "summary": "Driven mainly by live factor (-0.08), historical factor (+0.02)...",
    "contributions": [
      {"name": "live_factor", "value": 0.41, "contribution": -0.0794,
       "detail": "current speed reading pushes the forecast slower"}
    ]
  }
}
```

`Prediction` cannot be constructed without all three. That is not decoration: a dispatcher
shown "Level 1" has no way to weigh it against their own judgement without knowing the
model was 54% confident and what drove it. An unexplained number in an emergency system
either gets blindly obeyed or ignored entirely — both worse than no model.

`source` distinguishes `model` from `baseline` from `rule`, so a caller always knows
whether a trained estimator or the statistical fallback produced the answer.

---

## Phase 6 — the five estimators

| Estimator | Predicts | Baseline it must beat |
|---|---|---|
| `congestion` | speed factor N minutes ahead | live ⊕ historical blend, horizon-decayed |
| `eta_residual` | correction to the router's ETA | zero correction |
| `emergency_priority` | **advisory** Layer 6 level | the clinical rule (which stays authoritative) |
| `corridor_success` | P(preemption granted) | controller capability + recovery window |
| `clearance_time` | seconds of green needed | queue model from congestion × lanes |

### Why gradient-boosted trees, not a neural network

The brief listed TensorFlow or PyTorch. Every target here is tabular regression or small
classification over ~10 hand-built features — the regime where boosted trees beat neural
networks on accuracy, train in seconds on a laptop, and, decisively, admit **exact SHAP
attributions cheaply enough to explain every prediction** rather than a sampled few.

A DL framework would add 2–3 GB and real operational burden for no measured gain. There is
a genuine deep-learning case later — a spatio-temporal graph network over the road
network — but it needs months of real observations first. My recommendation stands:
scikit-learn now, revisit when data volume justifies it.

### Modelling the ETA *residual*, not the ETA

The router already encodes distance, speed limits, signal delay and the congestion
forecast. A model asked to reproduce all that would spend its capacity relearning geometry.
Asked only where the router is *systematically* wrong — junctions that always take longer,
a fleet that consistently beats its estimate — it learns from far less data. The correction
is capped at ±50% of the planned duration: a residual model confidently rewriting an ETA by
ten minutes is far more likely broken than insightful.

### The safety invariant: models advise, rules decide

`emergency_priority` **never sets a priority level.** The Rule-Based Emergency Engine does,
because the mapping from presentation to urgency is a clinical governance decision that must
be inspectable, versioned and signed off — not learned from whatever the last six months
happened to contain. Learning it would also bake in historical under-triage of exactly the
populations most likely to have been under-triaged.

What the model is for is flagging **disagreement**:

```json
{"prediction": 2, "context": {"rule_level": 2, "model_level": 1},
 "disagreement": "Model expects Level 1 but the clinical rule for Poisoning gives Level 2.
                  The rule stands; review whether the recorded category matches the presentation."}
```

`prediction` is always the rule's level. Four tests assert this directly.

The same applies to corridor prediction: an offline controller returns P=0 with confidence
1.0 and `source: "rule"`, regardless of what the model learned.

### Training governance

```
  congestion             4000 samples  MAE model=0.0448 baseline=0.0584  DEPLOY  (+23.3%)
  corridor_success       3000 samples  error-rate model=0.1400 baseline=0.1467  REJECT (+4.5%)
```

Nothing is written to disk unless it beats its baseline by ≥5%. A model that ties its
baseline adds a dependency, a failure mode and an explanation surface for nothing.
`corridor_success` was **rejected on its own numbers** and the statistical fallback kept.

Both errors are measured on the same held-out rows — a bug found during this phase was
splitting features and baselines separately, which compared performance on different data
and made the whole comparison meaningless.

`--synthetic` bootstraps the pipeline where history is thin. Those models are tagged
`synthetic` in their metadata and the command says plainly that they prove nothing about
real accuracy.

### Hospital recommendation: deliberately no SHAP

The recommender is a deterministic weighted sum over five named factors, and it already
publishes every one per candidate. Running an attribution method over arithmetic whose terms
are already visible would add ceremony, not insight — so the explanation reports the **real
weighted contributions**, which reconstruct the score exactly:

```
  +0.3400  capability scored 100% at weight 34%
  +0.2245  travel time scored 75% at weight 30%
  +0.1080  bed availability scored 60% at weight 18%
```

Confidence is the **margin over the runner-up**. A recommendation that barely beat second
place is genuinely less certain than one that dominated, and a crew deciding whether to
override deserves to know which they have.

---

## Phase 7 — the six detections

| Detection | How |
|---|---|
| Vehicle | YOLOv8 COCO classes, or the simulator |
| **Emergency vehicle** | fleet telemetry, visually corroborated |
| Traffic density | count → vehicles/lane-km → Greenshields speed |
| **Road block** | high occupancy + stillness + people |
| **Illegal parking** | still across N sweeps *while traffic moves* |
| Accident | people in carriageway + stopped + not congested |

Every finding carries confidence **and evidence**:

```
[accident] conf=0.70 sev=0.75
    Possible collision - pedestrians in carriageway with stopped traffic
      - 1 person(s) detected in the carriageway
      - traffic at 5% of free-flow speed
      - road is not congested, so stopped traffic is unexplained
      - 2 stationary vehicle(s) clustered
```

A control room asked to close a road on a camera's word needs to see *why*. An operator who
can read the reasoning dismisses a false positive in seconds instead of dispatching a unit.

### Emergency vehicle detection is telemetry-led, not visual

Deliberately **not** a visual classifier. COCO has no ambulance class, and a bespoke one
trained on a few hundred crops would be unreliable in exactly the conditions that matter —
rain, night, partial occlusion.

SEVPS already knows where every emergency vehicle is to within a few metres, several times
a minute. So position is the strong signal and vision *corroborates* it. That inverts the
usual arrangement and it is the right way round: a GPS fix is far better evidence than a
bounding box, and the box adds what GPS cannot — confirmation the vehicle is on **this**
carriageway rather than a parallel road ten metres away.

```
telemetry + large vehicle visible  → 0.97, corroborated
telemetry + empty frame            → 0.42, "possible occlusion or wrong approach"
```

### The judgement calls

Each detector's difficulty is a *discrimination*, not a detection:

- **Accident vs. a crowd.** People beside jammed traffic are a pavement or a crossing.
  People on a road that has stopped *without being congested* is unexplained. Same frame,
  confidence 0.70 vs 0.40.
- **Parked vs. waiting at a red light.** At a red light every vehicle looks parked. A parked
  vehicle stays still *while traffic around it moves*, across several sweeps. Below 40%
  free-flow speed the detector declines to call it at all.
- **Blockage vs. jam.** A jam is full of slowly moving vehicles; a blockage is full of
  stationary ones. Getting it wrong reroutes ambulances off a usable road.

Confidence is capped at 0.85 for accidents. This dispatches units — it should prompt a human
to look at the camera, not close a road on its own.

### Acting vs. noticing

Only findings at ≥0.55 confidence create road events. Below that they are recorded on the
analysis for a human. The bar to *act* is higher than the bar to *notice*, because a false
road closure reroutes ambulances away from a clear road.

Repeat findings **update** the open event rather than creating another — a camera reporting
the same jam every 30 seconds must not produce a hundred road events.

---

## API

```
GET  /api/v1/brain/ml/models/                 which models are trained
GET  /api/v1/brain/ml/congestion/?segment=1   speed factor + SHAP
GET  /api/v1/brain/ml/eta/<trip_id>/          router ETA + learned residual
POST /api/v1/brain/ml/priority/               advisory level + disagreement
GET  /api/v1/brain/ml/corridor/<signal_id>/   P(green granted)
POST /api/v1/brain/ml/hospital/               recommendation + weighted explanation

GET  /api/v1/network/cv/status/               backend + camera estate health
GET  /api/v1/network/cv/cameras/<id>/analyse/ analyse without persisting
POST /api/v1/network/cv/sweep/                analyse + ingest (traffic police)
GET  /api/v1/network/cv/emergency/            camera-corroborated fleet sightings
```

```bash
python manage.py train_models                       # from real history
python manage.py train_models --synthetic --save    # bootstrap the pipeline
python manage.py analyse_cameras --verbose-output
```

## Verified

```
252 backend tests (67 new: 31 ML + 36 CV)                      pass
congestion trained on 4,000 samples, SHAP attributions live    verified
corridor_success rejected by the governance rule               verified
priority prediction returns the rule level for all categories  verified
22-camera sweep, 0 failures                                    verified
sweep survives a dead feed                                     verified
6 detectors on hand-built frames, all discriminations correct  verified
```

## Not done

- **TensorFlow/PyTorch** — deferred with reasoning above. Revisit for spatio-temporal
  forecasting when real history exists.
- **Multi-frame tracking.** `track_id` is populated by the simulator but a plain YOLO
  `predict()` call has none, so illegal-parking detection needs `model.track()` before it
  works against real cameras. The code declines to invent ids rather than silently
  producing wrong parking findings.
- **Real camera validation.** The YOLO path is implemented and will run against RTSP, but
  has only been exercised against the simulated backend — no camera estate was available.
