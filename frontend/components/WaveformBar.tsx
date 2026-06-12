"use client";

/**
 * Waveform bar (FR-6), three modes:
 *   listening — bars follow the user's mic amplitude (warm amber)
 *   thinking  — three pulsing dots
 *   speaking  — bars follow the bot's audio amplitude (cool ice)
 *
 * Bars are drawn on canvas from an AnalyserNode at display refresh rate;
 * idle sessions show a dormant row of dots for compositional balance.
 */

import { useEffect, useRef } from "react";
import type { VoiceMode } from "@/lib/useAloudSession";

const BARS = 24;
const COLORS: Record<string, string> = {
  listening: "227, 164, 79", // amber
  speaking: "121, 200, 212", // ice
};

export function WaveformBar({
  active,
  mode,
  localTrack,
  botTrack,
}: {
  active: boolean;
  mode: VoiceMode;
  localTrack: MediaStreamTrack | null;
  botTrack: MediaStreamTrack | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const track = mode === "speaking" ? botTrack : localTrack;
  const showCanvas = active && mode !== "thinking" && track !== null;

  useEffect(() => {
    if (!showCanvas || !canvasRef.current || !track) return;

    const canvas = canvasRef.current;
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    const g = canvas.getContext("2d");
    if (!g) return;
    g.scale(dpr, dpr);

    const audioCtx = new AudioContext();
    const source = audioCtx.createMediaStreamSource(new MediaStream([track]));
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.7;
    source.connect(analyser);

    const bins = new Uint8Array(analyser.frequencyBinCount);
    // voice energy lives in the lower bins; ignore the empty top end
    const usable = Math.floor(bins.length * 0.6);
    const perBar = Math.max(1, Math.floor(usable / BARS));
    const heights = new Array(BARS).fill(0);
    const rgb = COLORS[mode] ?? COLORS.listening;

    const barW = 5;
    const gap = (cssW - BARS * barW) / (BARS - 1);
    const minH = 3;
    const maxH = cssH - 4;
    let raf = 0;

    const draw = () => {
      analyser.getByteFrequencyData(bins);
      g.clearRect(0, 0, cssW, cssH);
      g.shadowBlur = 10;
      g.shadowColor = `rgba(${rgb}, 0.45)`;
      for (let i = 0; i < BARS; i++) {
        let sum = 0;
        for (let j = 0; j < perBar; j++) sum += bins[i * perBar + j];
        const target = minH + (maxH - minH) * (sum / perBar / 255);
        heights[i] += (target - heights[i]) * 0.35; // ease toward target
        const h = heights[i];
        const x = i * (barW + gap);
        const y = (cssH - h) / 2;
        g.fillStyle = `rgba(${rgb}, ${0.55 + 0.45 * (h / maxH)})`;
        g.beginPath();
        g.roundRect(x, y, barW, h, barW / 2);
        g.fill();
      }
      raf = requestAnimationFrame(draw);
    };
    draw();

    return () => {
      cancelAnimationFrame(raf);
      source.disconnect();
      audioCtx.close().catch(() => {});
    };
  }, [showCanvas, track, mode]);

  if (active && mode === "thinking") {
    return (
      <div className="wave-area dots" aria-label="thinking">
        <span />
        <span />
        <span />
      </div>
    );
  }

  if (!showCanvas) {
    return (
      <div className="wave-area dormant" aria-hidden>
        {Array.from({ length: BARS }, (_, i) => (
          <i key={i} />
        ))}
      </div>
    );
  }

  return (
    <div className="wave-area">
      <canvas ref={canvasRef} className="wave-canvas" />
    </div>
  );
}
