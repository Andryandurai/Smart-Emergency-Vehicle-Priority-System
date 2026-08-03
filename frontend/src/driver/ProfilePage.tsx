/**
 * Tab 4 — Profile.
 *
 * Identity, not settings. There is no theme picker and no notification
 * tuning: a driver's account has nothing to configure, and a settings page
 * full of controls that do not apply teaches people to ignore the page that
 * one day will matter.
 *
 * The split between editable and read-only follows who is the authority on
 * each fact. The photograph, phone number, blood group and next of kin belong
 * to their owner. Staff id, driving qualification and base station are set by
 * whoever runs the roster, and are shown as plain figures rather than as
 * disabled inputs — a greyed-out field invites someone to go looking for the
 * permission to use it.
 */
import { useEffect, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { ApiError } from "@/api/client";
import { profile as profileApi } from "@/api/endpoints";
import type { StaffProfile } from "@/api/types";
import { DriverAvatar } from "@/driver/DriverShell";
import type { DriverOutletContext } from "@/driver/DriverShell";
import { DpError } from "@/driver/TakeoverPage";

export function ProfilePage() {
  const { shift } = useOutletContext<DriverOutletContext>();
  const [me, setMe] = useState<StaffProfile | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const [phone, setPhone] = useState("");
  const [blood, setBlood] = useState("");
  const [contact, setContact] = useState("");

  useEffect(() => {
    profileApi
      .mine()
      .then((profile) => {
        setMe(profile);
        setPhone(profile.phone);
        setBlood(profile.blood_group);
        setContact(profile.emergency_contact);
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Could not load your profile."),
      );
  }, []);

  const upload = async (file: File) => {
    setBusy("avatar");
    setError(null);
    try {
      setMe(await profileApi.uploadAvatar(file));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not upload that picture.");
    } finally {
      setBusy(null);
    }
  };

  const removePicture = async () => {
    setBusy("avatar");
    setError(null);
    try {
      setMe(await profileApi.removeAvatar());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the picture.");
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    setBusy("save");
    setError(null);
    setSaved(false);
    try {
      setMe(await profileApi.save({ phone, blood_group: blood, emergency_contact: contact }));
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save.");
    } finally {
      setBusy(null);
    }
  };

  if (!me) {
    return (
      <div className="dp-page">
        {error ? <div className="dp-error">{error}</div> : <div className="dp-loading">Loading profile…</div>}
      </div>
    );
  }

  return (
    <div className="dp-page">
      <div className="dp-profile-head">
        <button
          type="button"
          className="dp-avatar-btn"
          onClick={() => fileRef.current?.click()}
          disabled={busy !== null}
          title="Change your photograph"
        >
          <DriverAvatar profile={me} size={112} />
          <span className="dp-avatar-edit">{busy === "avatar" ? "…" : "Edit"}</span>
        </button>
        <div className="dp-profile-id">
          <h1>{me.name}</h1>
          <p>{me.qualification || "Ambulance Driver"}</p>
          <p className="dp-note">
            {[me.staff_id, me.base_station].filter(Boolean).join(" · ") || "No roster details"}
          </p>
          {me.avatar_url && (
            <button
              type="button"
              className="dp-linkbtn"
              disabled={busy !== null}
              onClick={() => void removePicture()}
            >
              Remove photograph
            </button>
          )}
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
          // Reset so re-picking the same file still fires a change event.
          event.target.value = "";
        }}
      />

      <DpError error={error} />

      <section className="dp-card">
        <h4>Service details</h4>
        <div className="dp-kv">
          <span>Staff ID</span>
          <b>{me.staff_id || "—"}</b>
        </div>
        <div className="dp-kv">
          <span>Username</span>
          <b>{me.username}</b>
        </div>
        <div className="dp-kv">
          <span>Driving qualification</span>
          <b>{me.qualification || "—"}</b>
        </div>
        <div className="dp-kv">
          <span>Base station</span>
          <b>{me.base_station || "—"}</b>
        </div>
        <div className="dp-kv">
          <span>Role</span>
          <b>{me.role_labels?.join(", ") || "Ambulance Driver"}</b>
        </div>
        <div className="dp-kv">
          <span>Email</span>
          <b>{me.email || "—"}</b>
        </div>
        <p className="dp-note">Service details are set by your roster administrator.</p>
      </section>

      <section className="dp-card">
        <h4>Current shift</h4>
        {shift ? (
          <>
            <div className="dp-kv">
              <span>Ambulance</span>
              <b>{shift.vehicle_callsign}</b>
            </div>
            <div className="dp-kv">
              <span>Vehicle number</span>
              <b>{shift.vehicle_registration || "—"}</b>
            </div>
            <div className="dp-kv">
              <span>Paramedic</span>
              <b>{shift.paramedic_detail?.name ?? "not yet crewed"}</b>
            </div>
            <div className="dp-kv">
              <span>State</span>
              <b>{shift.status_display}</b>
            </div>
          </>
        ) : (
          <p className="dp-note">You are off duty. Take over an ambulance to start a shift.</p>
        )}
      </section>

      <section className="dp-card">
        <h4>Your contact details</h4>
        <p className="dp-note">
          Used by the control room and, in an emergency, by whoever reaches you first.
        </p>

        <label htmlFor="dpPhone">Phone</label>
        <input id="dpPhone" value={phone} onChange={(event) => setPhone(event.target.value)} />

        <label htmlFor="dpBlood">Blood group</label>
        <input id="dpBlood" value={blood} onChange={(event) => setBlood(event.target.value)} />

        <label htmlFor="dpContact">Emergency contact</label>
        <input
          id="dpContact"
          value={contact}
          onChange={(event) => setContact(event.target.value)}
        />

        <button
          type="button"
          className="dp-btn primary"
          disabled={busy !== null}
          onClick={() => void save()}
        >
          {busy === "save" ? "Saving…" : saved ? "✓ Saved" : "Save"}
        </button>
      </section>
    </div>
  );
}
