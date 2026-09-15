/** @type {import('next').NextConfig} */
const DJANGO = process.env.DJANGO_ORIGIN ?? "http://localhost:8000";

const nextConfig = {
  // Django's URLs all end in a slash, and APPEND_SLASH cannot redirect a POST without
  // losing its body. Two things are needed to get a request through intact:
  //   1. stop Next redirecting the browser to the slash-less form, and
  //   2. re-append the slash on the rewrite destination, because :path* captures it away.
  skipTrailingSlashRedirect: true,

  // Proxy the API so the browser sees one origin. Session cookies then work with no
  // CORS config, and the CSRF cookie is readable for the X-CSRFToken header.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${DJANGO}/api/:path*/` }];
  },

  images: { unoptimized: true },
};

export default nextConfig;
