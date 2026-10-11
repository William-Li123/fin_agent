import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function Markdown({ children }: { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children: linkChildren }) => <a href={href} target="_blank" rel="noreferrer noopener">{linkChildren}</a>,
    img: () => <span className="muted">[外部图片已省略]</span>,
  }}>{children}</ReactMarkdown></div>;
}
