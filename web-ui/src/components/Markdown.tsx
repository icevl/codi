import { ComponentPropsWithoutRef, memo, useMemo } from "react";
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
const EXPQUOTE_BLOCK = /\x02EXPQUOTE_START\x02([\s\S]*?)\x02EXPQUOTE_END\x02/g;
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
  text = text.replace(/\x02/g, "");
  return text;
}

// Split into block-level chunks so unchanged blocks can skip re-parse on edit.
function splitBlocks(text: string): string[] {
  const blocks: string[] = [];
  let buf: string[] = [];
  let inFence = false;
  const flush = () => {
    if (buf.length > 0) {
      blocks.push(buf.join("\n"));
      buf = [];
    }
  };
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      inFence = !inFence;
      buf.push(line);
      continue;
    }
    if (!inFence && line.trim() === "") {
      flush();
      continue;
    }
    buf.push(line);
  }
  flush();
  return blocks;
}

const MD_COMPONENTS = {
  a: ({ href, children, ...rest }: ComponentPropsWithoutRef<"a">) => (
    <a href={href} target="_blank" rel="noopener noreferrer" {...rest}>
      {children}
    </a>
  ),
};

const MarkdownBlock = memo(function MarkdownBlock({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
      {text}
    </ReactMarkdown>
  );
});

export const Markdown = memo(function Markdown({ text }: Props) {
  const blocks = useMemo(() => splitBlocks(preprocess(text)), [text]);
  return (
    <div className="md">
      {blocks.map((b, i) => (
        <MarkdownBlock key={i} text={b} />
      ))}
    </div>
  );
});
