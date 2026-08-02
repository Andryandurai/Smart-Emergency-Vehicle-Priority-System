import { describe, expect, it } from "vitest";

import { formatValue } from "@/components/charts/primitives";

describe("formatValue", () => {
  it("renders seconds as minutes and seconds", () => {
    // "252" on an axis means nothing; "4m 12s" is an ambulance response time.
    expect(formatValue(252, "s")).toBe("4m 12s");
    expect(formatValue(120, "s")).toBe("2m");
    expect(formatValue(45, "s")).toBe("45s");
  });

  it("rolls over to hours for long durations", () => {
    expect(formatValue(7200, "s")).toBe("2h 0m");
    expect(formatValue(5460, "s")).toBe("1h 31m");
  });

  it("says 'not measured' rather than 0 for a null", () => {
    // The distinction the whole series design rests on: a day with no
    // measurable response time is not a day with a zero-second response.
    expect(formatValue(null, "s")).toBe("not measured");
    expect(formatValue(undefined, "trips")).toBe("not measured");
    expect(formatValue(0, "trips")).toBe("0 trips");
  });

  it("appends non-second units verbatim", () => {
    expect(formatValue(12, "trips")).toBe("12 trips");
    expect(formatValue(3, "corridors")).toBe("3 corridors");
  });

  it("keeps one decimal for small values and rounds large ones", () => {
    expect(formatValue(4.26, "trips")).toBe("4.3 trips");
    expect(formatValue(1234.6, "trips")).toBe("1235 trips");
  });

  it("handles a bare unit", () => {
    expect(formatValue(7, "")).toBe("7");
  });
});
