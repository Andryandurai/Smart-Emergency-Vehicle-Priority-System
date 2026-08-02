"""Computer-vision traffic analysis (Phase 7, feature 4.5).

Six detections, all from the same frame pass so a camera is decoded once:

===========================  =============================================
vehicle detection            what is on the road
emergency vehicle detection  is one of ours already here
traffic density              vehicles per lane-km, and the implied speed
road block detection         the carriageway is impassable
illegal parking detection    a stationary vehicle in a live lane
accident detection           the signature of a collision
===========================  =============================================

Two backends behind one interface:

``yolo``       real YOLOv8 inference through ultralytics + OpenCV.
``simulated``  deterministic synthetic detections driven by the segment's own
               state and the time of day. Lets the whole platform be
               demonstrated and tested without GPUs or live camera access.

Every inference returns confidence and the evidence behind it, in the same
envelope as the Phase 6 estimators. A detector that says "accident" without
saying how sure it is, and why, cannot be acted on by a control room - it will
either be blindly trusted or switched off, and both are worse than nothing.
"""
