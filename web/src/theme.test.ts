import { applyTheme, loadTheme, nextTheme } from "./theme";

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});

test("dark and light set the attribute, system removes it", () => {
  applyTheme("dark");
  expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  applyTheme("light");
  expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  applyTheme("system");
  expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
});

test("the choice is remembered", () => {
  applyTheme("dark");
  expect(loadTheme()).toBe("dark");
});

test("garbage in storage means system", () => {
  localStorage.setItem("rc-theme", "purple");
  expect(loadTheme()).toBe("system");
});

test("blocked storage does not break theming", () => {
  const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(() => applyTheme("dark")).not.toThrow();
  expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  spy.mockRestore();
});

test("the toggle cycles through all three", () => {
  expect(nextTheme("system")).toBe("light");
  expect(nextTheme("light")).toBe("dark");
  expect(nextTheme("dark")).toBe("system");
});
