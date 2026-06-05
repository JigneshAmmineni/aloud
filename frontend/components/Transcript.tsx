import { type SessionMessage } from "@/lib/session";

interface Props {
  messages: SessionMessage[];
}

export default function Transcript({ messages }: Props) {
  if (messages.length === 0) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {messages.map((msg, i) => (
        <div
          key={i}
          style={{
            alignSelf: msg.role === "user" ? "flex-end" : "flex-start",
            background: msg.role === "user" ? "#ebf8ff" : "#f7fafc",
            border: "1px solid #e2e8f0",
            borderRadius: 12,
            padding: "10px 14px",
            maxWidth: "80%",
          }}
        >
          <div style={{ fontSize: 11, color: "#999", marginBottom: 4 }}>
            {msg.role === "user" ? "You" : "Aloud"}
          </div>
          {msg.text}
        </div>
      ))}
    </div>
  );
}
