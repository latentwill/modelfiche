// Preferences must never prevent the workbench from opening in a restricted browser.
export function readPreference(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}

export function writePreference(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* Keep the current in-memory UI state. */ }
}

export function removePreference(key: string): void {
  try { localStorage.removeItem(key); } catch { /* Storage is optional. */ }
}
