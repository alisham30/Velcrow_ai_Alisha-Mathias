// Display only. All money crosses the wire as integer paise (spec 3), and the
// server sends ready-made *_display strings wherever it can; this exists for
// the few places the browser has only the number.
export function rupees(paise) {
  if (!Number.isInteger(paise)) {
    throw new Error(`money must be integer paise, got ${paise}`);
  }
  const sign = paise < 0 ? "−" : "";
  return (
    sign +
    "₹" +
    (Math.abs(paise) / 100).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}
