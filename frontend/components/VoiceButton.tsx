interface Props {
  active: boolean;
  onToggle: () => void;
}

export default function VoiceButton({ active, onToggle }: Props) {
  return (
    <button
      onClick={onToggle}
      style={{
        width: 80,
        height: 80,
        borderRadius: "50%",
        border: "none",
        background: active ? "#e53e3e" : "#3182ce",
        color: "#fff",
        fontSize: 14,
        cursor: "pointer",
        display: "block",
        margin: "0 auto 40px",
      }}
    >
      {active ? "Stop" : "Speak"}
    </button>
  );
}
