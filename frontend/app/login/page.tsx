"use client";

/**
 * /login (FR-30). One form serves sign-in and sign-up:
 *   email · password · [Sign in | Sign up] · [Continue with Google]
 * "Sign up" expands the form (preferred name + "Create account") instead of
 * creating anything — the visible email doubles as the confirmation step.
 * Errors render inline and never clear the fields. Error copy follows
 * FR-26(d): one generic, non-enumerating message for all failed sign-ins.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { FirebaseError } from "firebase/app";
import {
  createUserWithEmailAndPassword,
  GoogleAuthProvider,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signInWithPopup,
  updateProfile,
} from "firebase/auth";

import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";

const GENERIC_SIGNIN_ERROR =
  "Sign-in failed. Check your credentials, or try continuing with Google.";

// FR-26 error copy. Anything unlisted falls back to the generic message so
// the UI never becomes an enumeration oracle by accident.
function messageFor(e: unknown): string {
  if (e instanceof FirebaseError) {
    switch (e.code) {
      case "auth/email-already-in-use":
        return "Already registered — sign in instead.";
      case "auth/account-exists-with-different-credential":
        return "Sign in with your original method.";
      case "auth/invalid-email":
        return "That doesn't look like an email address.";
      case "auth/weak-password":
      case "auth/password-does-not-meet-requirements":
        return "Password is too short — use at least 6 characters.";
      case "auth/popup-closed-by-user":
      case "auth/cancelled-popup-request":
        return ""; // user changed their mind; not an error state
    }
  }
  return GENERIC_SIGNIN_ERROR;
}

export default function LoginPage() {
  const router = useRouter();
  const { user, loading } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [signupMode, setSignupMode] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Already signed in → into the app (FR-30).
  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, user, router]);

  const validate = (): boolean => {
    if (!email.trim() || !/.+@.+\..+/.test(email.trim())) {
      setError("Enter a valid email address.");
      return false;
    }
    if (!password) {
      setError("Enter a password.");
      return false;
    }
    return true;
  };

  const run = async (action: () => Promise<void>) => {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await action();
    } catch (e) {
      setError(messageFor(e));
    } finally {
      setBusy(false);
    }
  };

  const signIn = () =>
    run(async () => {
      if (!validate()) return;
      await signInWithEmailAndPassword(auth, email.trim(), password);
      router.replace("/");
    });

  const createAccount = () =>
    run(async () => {
      if (!validate()) return;
      const cred = await createUserWithEmailAndPassword(
        auth,
        email.trim(),
        password,
      );
      const preferred = name.trim().slice(0, 80);
      if (preferred) {
        await updateProfile(cred.user, { displayName: preferred });
        // The backend reads the name from the token's claim (FR-24) —
        // force a refresh so the claim exists from the first request.
        await cred.user.getIdToken(true);
      }
      // FR-25: verification email sent, access not gated on it.
      sendEmailVerification(cred.user).catch(() => {});
      router.replace("/");
    });

  const googleSignIn = () =>
    run(async () => {
      await signInWithPopup(auth, new GoogleAuthProvider());
      router.replace("/");
    });

  const resetPassword = () =>
    run(async () => {
      if (!email.trim()) {
        setError("Enter your email above first, then tap the reset link.");
        return;
      }
      try {
        await sendPasswordResetEmail(auth, email.trim());
      } catch {
        // Swallow everything: the confirmation below must be identical
        // whether or not the email exists (FR-27, non-enumerating).
      }
      setNotice("If an account exists for this email, a reset link has been sent.");
    });

  return (
    <main className="stage login-stage">
      <header className="masthead">
        <h1 className="wordmark">Aloud</h1>
        <p className="tagline">a place to think out loud</p>
      </header>

      <form
        className="login-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (busy) return;
          if (signupMode) createAccount();
          else signIn();
        }}
      >
        <input
          className="login-field"
          type="email"
          autoComplete="email"
          placeholder="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <input
          className="login-field"
          type="password"
          autoComplete={signupMode ? "new-password" : "current-password"}
          placeholder="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {signupMode && (
          <input
            className="login-field"
            type="text"
            autoComplete="nickname"
            maxLength={80}
            placeholder="preferred name — what should we call you?"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        )}

        {!signupMode && (
          <button
            type="button"
            className="login-link"
            onClick={resetPassword}
            disabled={busy}
          >
            Forgot password?
          </button>
        )}

        {signupMode ? (
          <button className="login-btn primary full" type="submit" disabled={busy}>
            Create account
          </button>
        ) : (
          <div className="login-btn-row">
            <button className="login-btn primary" type="submit" disabled={busy}>
              Sign in
            </button>
            <button
              type="button"
              className="login-btn"
              disabled={busy}
              onClick={() => {
                setError("");
                setNotice("");
                setSignupMode(true);
              }}
            >
              Sign up
            </button>
          </div>
        )}

        <button
          type="button"
          className="login-btn google full"
          onClick={googleSignIn}
          disabled={busy}
        >
          <svg viewBox="0 0 48 48" width="18" height="18" aria-hidden>
            <path
              fill="#EA4335"
              d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
            />
            <path
              fill="#4285F4"
              d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
            />
            <path
              fill="#FBBC05"
              d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"
            />
            <path
              fill="#34A853"
              d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
            />
          </svg>
          Sign in with Google
        </button>

        {signupMode && (
          <button
            type="button"
            className="login-link centered"
            disabled={busy}
            onClick={() => {
              setError("");
              setNotice("");
              setSignupMode(false);
            }}
          >
            Already have an account?
          </button>
        )}

        {error && <p className="error login-msg">{error}</p>}
        {notice && <p className="login-msg notice">{notice}</p>}
      </form>
    </main>
  );
}
