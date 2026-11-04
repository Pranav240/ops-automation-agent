import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  window.location.hash = "";
  vi.restoreAllMocks();
});
