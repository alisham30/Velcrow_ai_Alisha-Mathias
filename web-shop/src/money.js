// Every price on screen comes from the API's integer paise (spec 3, 11).
// Division here is display-only; no money math ever happens in the browser.
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
