import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Elevate AI Studio",
  description: "A local workspace for original video footage.",
};

export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
