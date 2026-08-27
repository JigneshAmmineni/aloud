// Firebase client init (auth only — no other Firebase products).
// This config is public by design: identifying, not secret. Server-side
// verification (backend/app/auth.py) is what actually protects anything.
import { getApps, initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: "AIzaSyAQodmdrfHpRjsbKWRfWWmVU8IyyTVXzIM",
  authDomain: "aloud-c74f5.firebaseapp.com",
  projectId: "aloud-c74f5",
  appId: "1:737131777937:web:f0642b3d29d06b198d5430",
};

const app = getApps()[0] ?? initializeApp(firebaseConfig);

export const auth = getAuth(app);
