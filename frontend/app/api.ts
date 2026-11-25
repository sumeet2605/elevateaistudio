export const api = process.env.NEXT_PUBLIC_ELEVATE_API_URL ?? "http://127.0.0.1:8000";

export async function request<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const response = await fetch(api + endpoint, options).catch(() => {
    throw new Error("Couldn’t reach the local API. Check that the backend is running.");
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(typeof body?.detail === "string" ? body.detail : "The request could not be completed.");
  }
  return response.json();
}
