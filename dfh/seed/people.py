"""Deterministic synthetic people data for attendance, rosters, leave, onboarding and hiring."""
import random
from datetime import date, datetime, time, timedelta
from sqlalchemy import text

NOW = datetime(2026, 10, 31, 9, 0)


def run(eng, seed=42):
    rng = random.Random(seed + 10)
    today = NOW.date()
    with eng.begin() as c:
        employees = [dict(r._mapping) for r in c.execute(text("select id,location_id,department_id,designation_id,join_date,status from employee order by id"))]
        active = [e for e in employees if e["status"] == "active"]
        shifts = [dict(r._mapping) for r in c.execute(text("select id,code,start_time,end_time from shift order by id"))]
        leave_types = [dict(r._mapping) for r in c.execute(text("select id,code from leave_type order by id"))]
        shift_by_id = {s["id"]: s for s in shifts}
        attendance, roster = [], []
        for e in active:
            joined = e["join_date"] if isinstance(e["join_date"], date) else date.fromisoformat(str(e["join_date"]))
            for offset in range(13, -1, -1):
                day = today - timedelta(days=offset)
                if day.weekday() == 6 or day < joined:
                    continue
                roll = rng.random()
                status = "absent" if roll < .025 else "late" if roll < .105 else "present"
                sh = rng.choice(shifts)
                start = datetime.combine(day, time.fromisoformat(sh["start_time"]))
                punch_in = None if status == "absent" else start + timedelta(minutes=rng.randint(0, 35) if status == "late" else rng.randint(-8, 8))
                hours = None if status == "absent" else round(rng.uniform(7.0, 9.2), 2)
                punch_out = None if status == "absent" else punch_in + timedelta(hours=hours)
                attendance.append(dict(employee_id=e["id"], date=day, punch_in=punch_in, punch_out=punch_out, source="seed", location_id=e["location_id"], hours=hours, status=status))
            for offset in range(0, 14):
                day = today + timedelta(days=offset)
                if day.weekday() == 6 or day < joined:
                    continue
                sh = rng.choice(shifts)
                roster.append(dict(employee_id=e["id"], date=day, shift_id=sh["id"], location_id=e["location_id"]))
        if attendance:
            c.execute(text("insert into attendance(employee_id,date,punch_in,punch_out,source,location_id,hours,status) values (:employee_id,:date,:punch_in,:punch_out,:source,:location_id,:hours,:status)"), attendance)
        if roster:
            c.execute(text("insert into roster(employee_id,date,shift_id,location_id) values (:employee_id,:date,:shift_id,:location_id)"), roster)

        leave_rows = []
        for _ in range(135):
            e = rng.choice(active)
            start = today + timedelta(days=rng.randint(-70, 28))
            span = rng.choices([1, 2, 3, 4, 5], [35, 25, 20, 12, 8])[0]
            state = rng.choices(["approved", "pending", "rejected"], [55, 28, 17])[0]
            leave_rows.append(dict(employee_id=e["id"], leave_type_id=rng.choice(leave_types)["id"], from_date=start,
                                   to_date=start + timedelta(days=span-1), status=state, is_wfh=(rng.random() < .12),
                                   approver_id=(1 if state != "pending" else None)))
        c.execute(text("insert into leave_request(employee_id,leave_type_id,from_date,to_date,status,is_wfh,approver_id) values (:employee_id,:leave_type_id,:from_date,:to_date,:status,:is_wfh,:approver_id)"), leave_rows)

        onboarding = []
        recent = [e for e in active if (today - (e["join_date"] if isinstance(e["join_date"], date) else date.fromisoformat(str(e["join_date"])))).days <= 90]
        task_names = ["Collect identity and address documents", "Issue laptop, POS or store equipment", "Complete policy and safety induction", "Role and product training"]
        for e in recent:
            joined = e["join_date"] if isinstance(e["join_date"], date) else date.fromisoformat(str(e["join_date"]))
            for i, task in enumerate(task_names):
                due = joined + timedelta(days=[2, 5, 10, 21][i])
                done = (datetime.combine(due, time(17, 0)) if due <= today and rng.random() < .78 else None)
                onboarding.append(dict(employee_id=e["id"], task=task, due_date=due, done_at=done))
        if onboarding:
            c.execute(text("insert into onboarding_task(employee_id,task,due_date,done_at) values (:employee_id,:task,:due_date,:done_at)"), onboarding)

        departments = [dict(r._mapping) for r in c.execute(text("select id,name from department order by id"))]
        locations = [dict(r._mapping) for r in c.execute(text("select id,name,type from location where type in ('store','warehouse','office') order by id"))]
        titles = ["Store Manager", "Retail Sales Associate", "Warehouse Associate", "Procurement Executive", "People Operations Specialist",
                  "Finance Analyst", "Demand Planner", "Customer Experience Executive", "Quality and Compliance Lead", "Regional Operations Manager"]
        jobs = []
        for i, title in enumerate(titles):
            dept = next((d for d in departments if any(k in title.lower() for k in d["name"].lower().split()[:1])), rng.choice(departments))
            loc = rng.choice(locations)
            posted = today - timedelta(days=rng.randint(2, 50))
            status = "open" if i < 7 else "closed" if i < 9 else "paused"
            jobs.append(dict(title=title, department_id=dept["id"], location_id=loc["id"], status=status, posted_on=posted,
                             closes_on=posted + timedelta(days=30), description=f"Demo role: {title}. Own day-to-day execution, service quality and measurable outcomes."))
        c.execute(text("insert into job_posting(title,department_id,location_id,status,posted_on,closes_on,description) values (:title,:department_id,:location_id,:status,:posted_on,:closes_on,:description)"), jobs)
        job_ids = [r[0] for r in c.execute(text("select id from job_posting order by id desc limit 10"))][::-1]
        applicant_names = ["Aarav Mehta","Ananya Rao","Ishaan Shah","Diya Nair","Kabir Iyer","Sara Khan","Vihaan Patel","Mira Joshi","Arjun Das","Aisha Menon",
                           "Reyansh Gupta","Myra Kulkarni","Advait Reddy","Kiara Bose","Neil Thomas","Zoya Kapoor","Rohan Bhat","Tara Sinha","Dev Malhotra","Ira Fernandes"]
        applicants = []
        for i in range(112):
            applicants.append(dict(job_id=rng.choice(job_ids), name=rng.choice(applicant_names) + f" {i+1:03d}",
                                   stage=rng.choices(["applied","screening","interview","offer","hired","rejected"],[30,22,18,7,5,18])[0],
                                   applied_on=today-timedelta(days=rng.randint(0,55)),source=rng.choice(["Referral","Careers page","LinkedIn","Job board","Walk-in"])))
        c.execute(text("insert into applicant(job_id,name,stage,applied_on,source) values (:job_id,:name,:stage,:applied_on,:source)"), applicants)
        announcements = [
            ("Diwali trading hours","Store teams should confirm local opening hours and staffing for the festive period.","All"),
            ("Food safety refresher","Complete the handling, storage and allergen-awareness refresher before the next roster cycle.","All"),
            ("Cycle count week","Warehouse and store managers should complete their assigned priority SKU counts.","Operations"),
            ("Benefits enrolment window","Review your employee details and submit any benefits changes within the published window.","All"),
            ("New product knowledge","The updated nuts, dates and gifting range guide is available for team briefings.","Retail"),
            ("Quarterly town hall","Department heads will share the next-quarter operating priorities and customer experience goals.","Office"),
            ("Attendance reminder","Please record attendance accurately at the start and end of each shift.","All"),
            ("Customer-first week","Share one practical improvement that makes shopping simpler for customers.","All"),
        ]
        c.execute(text("insert into announcement(title,body,created_at,audience) values (:title,:body,:created_at,:audience)"),
                  [dict(title=t,body=b,created_at=NOW-timedelta(days=i*3),audience=a) for i,(t,b,a) in enumerate(announcements)])

        # Link demo logins to the corresponding seeded employee records for self-service expansion.
        links = {
            "ceo": "select e.id from employee e join designation g on g.id=e.designation_id where g.title='CEO' order by e.id limit 1",
            "finance": "select e.id from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id where d.name='Finance' and g.title='Head of Department' order by e.id limit 1",
            "procurement": "select e.id from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id where d.name='Procurement' and g.title='Head of Department' order by e.id limit 1",
            "hr": "select e.id from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id where d.name='HR' and g.title='Head of Department' order by e.id limit 1",
            "storemgr": "select e.id from employee e join designation g on g.id=e.designation_id where g.title='Store Manager' order by e.id limit 1",
            "cashier": "select e.id from employee e join designation g on g.id=e.designation_id join location l on l.id=e.location_id where g.title='Cashier' and l.id=(select scope_id from app_user where username='cashier') order by e.id limit 1",
            "warehouse": "select e.id from employee e join designation g on g.id=e.designation_id where g.title='Warehouse Manager' and e.location_id=(select scope_id from app_user where username='warehouse') order by e.id limit 1",
        }
        for username, query in links.items():
            eid = c.execute(text(query)).scalar()
            if eid:
                c.execute(text("update app_user set employee_id=:e where username=:u"), {"e":eid,"u":username})
    return dict(employees=len(active), attendance=len(attendance), roster=len(roster), leave_requests=len(leave_rows),
                onboarding_tasks=len(onboarding), jobs=len(jobs), applicants=len(applicants), announcements=len(announcements))
