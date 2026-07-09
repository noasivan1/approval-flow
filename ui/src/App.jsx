const sections = [
  { title: "Submit Invoice", note: "Invoice submission form will be added in the next step." },
  { title: "Check Status", note: "Status lookup will show the current approval state by tracking id." },
  { title: "Approver Queue", note: "Escalated items will appear here for human review." },
  { title: "Approve / Reject", note: "Approvers will be able to approve, reject, or send items back." },
  { title: "Dashboard", note: "Basic approval and escalation metrics will be shown here." },
];

export default function App() {
  return (
    <main className="app">
      <header className="app__header">
        <h1>ApprovalFlow</h1>
        <p>Invoice &amp; Expense Approval Platform</p>
      </header>

      <div className="grid">
        {sections.map((section) => (
          <section key={section.title} className="card">
            <h2>{section.title}</h2>
            <p>{section.note}</p>
          </section>
        ))}
      </div>
    </main>
  );
}
