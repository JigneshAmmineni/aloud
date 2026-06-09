import type { AgentStatus } from "@/lib/session";

interface Props {
  status: AgentStatus | "disconnected";
  onSpeak: () => void;
  onStop: () => void;
}

const STATE: Record<
  AgentStatus | "disconnected",
  { bg: string; label: string; disabled: boolean; animation?: string }
> = {
  disconnected: { bg: "#3182ce", label: "Speak",  disabled: false },
  connecting:   { bg: "#a0aec0", label: "···",    disabled: true  },
  idle:         { bg: "#3182ce", label: "Speak",  disabled: false },
  recording:    { bg: "#e53e3e", label: "Stop",   disabled: false, animation: "pulse-red 1.5s ease-in-out infinite"   },
  processing:   { bg: "#718096", label: "···",    disabled: true  },
  speaking:     { bg: "#38a169", label: "♪",      disabled: true,  animation: "pulse-green 1.5s ease-in-out infinite" },
  error:        { bg: "#3182ce", label: "Speak",  disabled: false },
};

export default function VoiceButton({ status, onSpeak, onStop }: Props) {
  const { bg, label, disabled, animation } = STATE[status];
  const onClick = status === "recording" ? onStop : onSpeak;

  return (
    <>
      <style>{`
        @keyframes pulse-red   { 0%,100% { box-shadow: 0 0 0 0 rgba(229,62,62,0.5);  } 50% { box-shadow: 0 0 0 14px rgba(229,62,62,0);  } }
        @keyframes pulse-green { 0%,100% { box-shadow: 0 0 0 0 rgba(56,161,105,0.5); } 50% { box-shadow: 0 0 0 14px rgba(56,161,105,0); } }
      `}</style>
      <button
        onClick={disabled ? undefined : onClick}
        disabled={disabled}
        style={{
          width: 80,
          height: 80,
          borderRadius: "50%",
          border: "none",
          background: bg,
          color: "#fff",
          fontSize: label === "···" ? 20 : label === "♪" ? 24 : 14,
          cursor: disabled ? "wait" : "pointer",
          display: "block",
          margin: "0 auto 24px",
          transition: "background 0.25s ease",
          opacity: disabled ? 0.75 : 1,
          animation: animation ?? "none",
        }}
      >
        {label}
      </button>
    </>
  );
}
