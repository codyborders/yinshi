import { renderHook, act } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { useMobileDrawer } from "../useMobileDrawer";

describe("useMobileDrawer", () => {
  it("starts closed", () => {
    const { result } = renderHook(() => useMobileDrawer());
    expect(result.current.open).toBe(false);
    expect(result.current.panelClassName).toContain("-translate-x-full");
  });

  it("opens on toggle", () => {
    const { result } = renderHook(() => useMobileDrawer());
    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);
    expect(result.current.panelClassName).toContain("translate-x-0");
    expect(result.current.panelClassName).not.toContain("-translate-x-full");
  });

  it("closes on close()", () => {
    const { result } = renderHook(() => useMobileDrawer());
    act(() => result.current.toggle());
    expect(result.current.open).toBe(true);
    act(() => result.current.close());
    expect(result.current.open).toBe(false);
  });

  it("includes md:static and md:translate-x-0 for desktop", () => {
    const { result } = renderHook(() => useMobileDrawer());
    expect(result.current.panelClassName).toContain("md:static");
    expect(result.current.panelClassName).toContain("md:translate-x-0");
  });
});
