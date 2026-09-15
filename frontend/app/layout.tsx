import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Zory pipeline · Review",
  description: "Product category and dimension verification",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
