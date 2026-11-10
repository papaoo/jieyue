interface BrandMarkProps {
  className?: string;
}

/** Original offset step planes. No third-party brand assets. */
export const BrandMark = ({ className }: BrandMarkProps) => (
  <svg
    className={className}
    viewBox="0 0 64 64"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    aria-hidden="true"
    focusable="false"
  >
    <path d="M8 42h17V25h18V8h13v30H38v18H8V42Z" fill="currentColor" />
    <path d="M8 27h13V10h18v11H29v18H8V27Z" fill="currentColor" opacity=".48" />
  </svg>
);
