/**
 * Profile — replaces Settings for the paramedic portal.
 *
 * Identity only. There is no theme picker, no notification tuning, no data
 * export: a paramedic's account has nothing to configure, and a settings page
 * full of controls that do not apply teaches people to ignore the page that
 * one day will matter.
 *
 * The picture is the one genuinely editable thing, because it is the one
 * piece of this record its owner is the authority on. Staff id, qualification
 * and base station are set by whoever runs the roster and are shown read-only.
 */
import { useEffect, useRef, useState } from "react";

import { ApiError } from "@/api/client";
import { profile as profileApi } from "@/api/endpoints";
import type { StaffProfile } from "@/api/types";
import { Avatar } from "@/paramedic/ParamedicShell";

export function ProfilePage() {
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
      .then((p) => {
        setMe(p);
        setPhone(p.phone);
        setBlood(p.blood_group);
        setContact(p.emergency_contact);
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Could not load your profile."),
      );
  }, []);

  const pick = () => fileRef.current?.click();

  const upload = async (file: File) => {
    setBusy("avatar");
    setError(null);
    try {
      setMe(await profileApi.uploadAvatar(file));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not upload that picture.",
      );
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
      setMe(
        await profileApi.save({
          phone,
          blood_group: blood,
          emergency_contact: contact,
        }),
      );
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
      <div className="pm-page">
        {error ? <div className="pm-error">{error}</div> : <p>Loading profile…</p>}
      </div>
    );
  }

  return (
    <div className="pm-page">
      <div className="pm-profile-head">
        <button
          type="button"
          className="pm-avatar-btn"
          onClick={pick}
          disabled={busy !== null}
          title="Change profile picture"
        >
          <Avatar profile={me} size={104} />
          <span className="pm-avatar-edit">{busy === "avatar" ? "…" : "Edit"}</span>
        </button>
        <h1>{me.name}</h1>
        <p>{me.qualification || "Paramedic"}</p>
        {me.avatar_url && (
          <button
            type="button"
            className="pm-linkbtn"
            disabled={busy !== null}
            onClick={() => void removePicture()}
          >
            Remove picture
          </button>
        )}
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

      {error && <div className="pm-error">{error}</div>}

      <section className="pm-card">
        <h3>Service details</h3>
        <div className="pm-kv">
          <span>Staff ID</span>
          <b>{me.staff_id || "—"}</b>
        </div>
        <div className="pm-kv">
          <span>Username</span>
          <b>{me.username}</b>
        </div>
        <div className="pm-kv">
          <span>Qualification</span>
          <b>{me.qualification || "—"}</b>
        </div>
        <div className="pm-kv">
          <span>Base station</span>
          <b>{me.base_station || "—"}</b>
        </div>
        <div className="pm-kv">
          <span>Role</span>
          <b>{me.role_labels?.join(", ") || "Paramedic"}</b>
        </div>
        <p className="pm-note">Service details are set by your roster administrator.</p>
      </section>

      <section className="pm-card">
        <h3>Your contact details</h3>
        <label htmlFor="phone">Phone</label>
        <input id="phone" value={phone} onChange={(e) => setPhone(e.target.value)} />

        <label htmlFor="blood">Blood group</label>
        <input id="blood" value={blood} onChange={(e) => setBlood(e.target.value)} />

        <label htmlFor="contact">Emergency contact</label>
        <input id="contact" value={contact} onChange={(e) => setContact(e.target.value)} />

        <button
          type="button"
          className="pm-btn primary"
          disabled={busy !== null}
          onClick={() => void save()}
        >
          {busy === "save" ? "Saving…" : saved ? "✓ Saved" : "Save"}
        </button>
      </section>
    </div>
  );
}
