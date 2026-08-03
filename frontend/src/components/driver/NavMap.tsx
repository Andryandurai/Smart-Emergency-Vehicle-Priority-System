/**
 * First-person navigation map.
 *
 * Deliberately not the operations map with a different sidebar. A control
 * room reads a north-up plan view of the whole city; a driver at 60 km/h
 * reads the next 400 metres, and needs "the road ahead is at the top of the
 * screen" to be true without thinking about it. So the map rotates to the
 * heading and the vehicle sits low in the frame with the route running up.
 *
 * Rotation is applied to Leaflet's map pane by CSS transform rather than by
 * reprojecting: it costs nothing per frame, keeps every existing layer
 * working unchanged, and the counter-rotation on markers is one more
 * transform rather than a second rendering path.
 */
import { useEffect, useRef } from "react";
import { useMap } from "react-leaflet";

/**
 * Rotates the map so the direction of travel points up.
 *
 * Heading is smoothed across the shortest angular path: a vehicle crossing
 * north flips between 359° and 1°, and interpolating those the long way spins
 * the whole map through a full turn at exactly the moment a driver is
 * looking for a junction.
 */
export function HeadingRotation({
  heading,
  enabled = true,
}: {
  heading: number;
  enabled?: boolean;
}) {
  const map = useMap();
  const smoothed = useRef(heading);

  useEffect(() => {
    const container = map.getContainer();
    const pane = container.querySelector<HTMLElement>(".leaflet-map-pane");
    if (!pane) return;

    if (!enabled) {
      pane.style.transform = pane.style.transform.replace(/rotate\([^)]*\)/, "");
      container.classList.remove("nav-rotating");
      container.style.removeProperty("--nav-heading");
      return;
    }

    // Shortest-path smoothing, keeping the accumulated angle unwrapped so the
    // CSS transform never has to jump.
    const delta = ((heading - smoothed.current + 540) % 360) - 180;
    smoothed.current += delta;
    const angle = -smoothed.current;

    container.classList.add("nav-rotating");
    container.style.setProperty("--nav-heading", `${angle}deg`);
    // Counter-rotation for markers and tooltips is done in CSS from the same
    // custom property, so text stays upright while the road turns.
  }, [map, heading, enabled]);

  return null;
}

/**
 * Keeps the vehicle in the lower third of the frame rather than centred.
 *
 * A centred vehicle wastes half the screen on road already driven. Offsetting
 * the centre forward along the heading buys roughly twice the look-ahead at
 * the same zoom, which is the whole difference between seeing the next
 * junction and arriving at it.
 */
export function ChaseCamera({
  position,
  heading,
  enabled = true,
  lookAheadPx = 150,
}: {
  position: [number, number] | null;
  heading: number;
  enabled?: boolean;
  lookAheadPx?: number;
}) {
  const map = useMap();
  const interacting = useRef(0);

  useEffect(() => {
    const touch = () => {
      interacting.current = Date.now();
    };
    map.on("dragstart", touch);
    map.on("zoomstart", touch);
    return () => {
      map.off("dragstart", touch);
      map.off("zoomstart", touch);
    };
  }, [map]);

  useEffect(() => {
    if (!enabled || !position) return;
    // A driver who has deliberately panned - to check a diversion, say - keeps
    // control for a few seconds rather than fighting the camera.
    if (Date.now() - interacting.current < 5000) return;

    // Projection space, not container space. Leaflet has no idea the
    // container is CSS-rotated, so `containerPointToLatLng` round-trips
    // through a frame that does not match what is drawn. `project`/
    // `unproject` are pure map maths and immune to any later transform.
    const zoom = map.getZoom();
    const point = map.project(position, zoom);

    // The offset must be expressed in map space and then rotated, because the
    // container is displayed rotated by -heading. Offsetting straight down in
    // map coordinates only reads as "behind the vehicle" at heading 0; at 180
    // it puts the vehicle at the top of the screen and the driver is looking
    // at road already travelled.
    //
    // Screen vector (0, +d) requires map vector rotate(+heading)·(0, d).
    const theta = (heading * Math.PI) / 180;
    const centre = map.unproject(
      [
        point.x + lookAheadPx * Math.sin(theta),
        point.y - lookAheadPx * Math.cos(theta),
      ],
      zoom,
    );

    // Jump rather than glide when the vehicle is not on screen at all.
    //
    // This is the whole reason the console used to open on an empty view: a
    // telemetry fix arrives roughly every second, each one restarted a 0.9s
    // eased pan, and each restart began again from wherever the last had got
    // to. Starting from the city-centre default the animation could never
    // finish, so the map crept a few pixels a second toward an ambulance it
    // never reached. Animate only once already nearby; otherwise snap.
    if (!map.getBounds().pad(0.35).contains(position)) {
      map.setView(centre, zoom, { animate: false });
      return;
    }

    // Already tracking: ignore sub-pixel jitter so the view is not
    // permanently mid-animation on a stationary vehicle.
    const drift = map.getCenter().distanceTo(centre);
    if (drift < 8) return;
    map.panTo(centre, { animate: true, duration: 0.9, easeLinearity: 0.4 });
  }, [map, position, heading, enabled, lookAheadPx]);

  return null;
}
