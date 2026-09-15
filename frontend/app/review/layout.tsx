import { ReviewProvider } from "@/components/ReviewProvider";

export default function ReviewLayout({ children }: { children: React.ReactNode }) {
  return <ReviewProvider>{children}</ReviewProvider>;
}
