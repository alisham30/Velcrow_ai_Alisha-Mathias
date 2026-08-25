import React from "react";

export default function QtyStepper({ value, onChange, min = 1, disabled = false, compact = false }) {
  const size = compact ? "h-7 w-7 text-sm" : "h-9 w-9";
  return (
    <div className="inline-flex items-center rounded-lg border border-line bg-card">
      <button
        type="button"
        aria-label="decrease quantity"
        disabled={disabled || value <= min}
        onClick={() => onChange(value - 1)}
        className={`${size} font-bold text-brand disabled:text-line`}
      >
        &minus;
      </button>
      <span className={`min-w-8 text-center font-semibold ${compact ? "text-sm" : ""}`}>{value}</span>
      <button
        type="button"
        aria-label="increase quantity"
        disabled={disabled}
        onClick={() => onChange(value + 1)}
        className={`${size} font-bold text-brand disabled:text-line`}
      >
        +
      </button>
    </div>
  );
}
