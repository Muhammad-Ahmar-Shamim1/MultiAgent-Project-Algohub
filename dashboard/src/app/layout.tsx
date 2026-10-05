import type { Metadata, Viewport } from "next";

import { Nav } from "@/components/nav";
import { themeScript } from "@/components/theme-toggle";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: { default: "AlgoHub Dashboard", template: "%s · AlgoHub" },
  description: "AlgoHub Web Department control room",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-screen antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-background focus:p-2"
        >
          Skip to content
        </a>
        <Providers>
          <Nav />
          <main id="main" className="mx-auto max-w-7xl px-4 py-6">
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
