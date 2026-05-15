import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  text: string;
}

// Codexbot's transcript parser wraps long tool output in sentinel markers
// that map to Telegram's expandable blockquote feature. The STX bytes are
// invisible in the browser but the literal "EXPQUOTE_START/END" text
// survives JSON serialization — translate them into a markdown blockquote
// so the web renders the same intent as the bot.
const EXPQUOTE_BLOCK = /EXPQUOTE_START([\s\S]*?)EXPQUOTE_END/g;
// Defensive fallback: same markers without the STX bytes (e.g. when something
// upstream stripped control chars).
const EXPQUOTE_BLOCK_PLAIN = /EXPQUOTE_START([\s\S]*?)EXPQUOTE_END/g;

function preprocess(raw: string): string {
  let text = raw;
  const replacer = (_match: string, inner: string) => {
    const lines = inner.trim().split("\n");
    return "\n" + lines.map((l) => `> ${l}`).join("\n") + "\n";
  };
  text = text.replace(EXPQUOTE_BLOCK, replacer);
  text = text.replace(EXPQUOTE_BLOCK_PLAIN, replacer);
  // Strip any remaining stray STX bytes.
  text = text.replace(//g, "");
  return text;
}

// Memoized: parsing markdown for every bubble on every keystroke in the
// composer is by far the dominant frame cost — text is a primitive string,
// so referential equality is the right cache key here.
export const Markdown = memo(function Markdown({ text }: Props) {
  const cleaned = preprocess(text);
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        // Keep anchors safe: open external links in a new tab.
        components={{
          a: ({ href, children, ...rest }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
              {children}
            </a>
          ),
        }}
      >
        {cleaned}
      </ReactMarkdown>
    </div>
  );
});
