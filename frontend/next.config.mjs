/** @type {import('next').NextConfig} */
const DJANGO = process.env.DJANGO_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  // Django's URLs all end in a slash, and APPEND_SLASH cannot redirect a POST without
  // losing its body. Two things are needed to get a request through intact:
  //   1. stop Next redirecting the browser to the slash-less form, and
  //   2. re-append the slash on the rewrite destination, because :path* captures it away.
  skipTrailingSlashRedirect: true,

  // Next's built-in gzip wraps every response this server sends, including a proxied
  // one — and Node's gzip stream buffers small writes instead of flushing them, so the
  // SSE endpoint's tiny event frames (pipeline/streaming.py) would sit in a compression
  // buffer and never reach the browser, even though the connection looks perfectly
  // healthy. A real deployment should compress at the reverse-proxy/CDN layer anyway
  // (the standard place for it), not duplicate that in the Node process — so this is
  // the correct fix, not a workaround.
  compress: false,

  // Proxy the API so the browser sees one origin. Session cookies then work with no
  // CORS config, and the CSRF cookie is readable for the X-CSRFToken header.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${DJANGO}/api/:path*/` }];
  },

  images: { unoptimized: true },
};

export default nextConfig;
