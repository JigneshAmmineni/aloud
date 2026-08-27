"use client";

/**
 * Auth context (REQUIREMENTS §4.8). Wraps the app once (in layout.tsx);
 * everything reads identity from here. Tokens: the Firebase SDK silently
 * refreshes the 1h ID token; getToken() always returns a current one, and
 * authedFetch attaches it as the Bearer header (FR-23's client half).
 */

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { getIdTokenResult, onIdTokenChanged, type User } from "firebase/auth";

import { auth } from "@/lib/firebase";

type AuthState = {
  user: User | null;
  loading: boolean; // true until the first onIdTokenChanged fires
  isAdmin: boolean; // cosmetic (drives the Admin nav link); server enforces
};

const AuthContext = createContext<AuthState>({
  user: null,
  loading: true,
  isAdmin: false,
});

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    loading: true,
    isAdmin: false,
  });

  useEffect(() => {
    return onIdTokenChanged(auth, async (user) => {
      let isAdmin = false;
      if (user) {
        const result = await getIdTokenResult(user);
        isAdmin = result.claims.admin === true;
      }
      setState({ user, loading: false, isAdmin });
    });
  }, []);

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

/** Current ID token, or null when signed out. */
export async function getToken(): Promise<string | null> {
  const user = auth.currentUser;
  return user ? user.getIdToken() : null;
}

/** fetch with the Bearer token attached — the only way the app calls the API. */
export async function authedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const token = await getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}
