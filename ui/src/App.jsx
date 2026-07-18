import SubmitInvoiceForm from "./SubmitInvoiceForm.jsx";
import StatusLookup from "./StatusLookup.jsx";
import ApproverQueue from "./ApproverQueue.jsx";
import Dashboard from "./Dashboard.jsx";
import Notifications from "./Notifications.jsx";

const placeholders = [
  { title: "Approve / Reject", note: "Handled inline in the Approver Queue above." },
];

export default function App() {
  return (
    <main className="app">
      <header className="app__header">
        <h1>ApprovalFlow</h1>
        <p>Invoice &amp; Expense Approval Platform</p>
      </header>

      <div className="grid">
        <section className="card card--wide">
          <h2>Submit Invoice</h2>
          <SubmitInvoiceForm />
        </section>

        <section className="card card--wide">
          <h2>Check Status</h2>
          <StatusLookup />
        </section>

        <section className="card card--wide">
          <h2>Approver Queue</h2>
          <ApproverQueue />
        </section>

        <section className="card card--wide">
          <h2>Notifications</h2>
          <Notifications />
        </section>

        <section className="card card--wide">
          <h2>Dashboard</h2>
          <Dashboard />
        </section>

        {placeholders.map((section) => (
          <section key={section.title} className="card">
            <h2>{section.title}</h2>
            <p>{section.note}</p>
          </section>
        ))}
      </div>
    </main>
  );
}
