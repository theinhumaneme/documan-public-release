// Read-only load test against a running Documan API.
//
// Runs on the API's own host, against 127.0.0.1, so it measures the application
// rather than Cloudflare or the link to it. That also means the generator shares
// the box's two cores with the thing it is measuring — the numbers below are a
// floor, not a ceiling, and the gap widens as VUs climb.
//
// Every request is a GET and nothing here writes. Safe to repeat.
//
// The stages ramp rather than jump so a limit shows up as a knee in the curve,
// and the thresholds abort the run instead of riding a failing service down —
// this is production, sharing 954 MB with Postgres, Redis and Meilisearch.
//
//   k6 run -e BASE=http://127.0.0.1:8082 loadtest-read.js
//
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const BASE = `${__ENV.BASE || 'http://127.0.0.1:8082'}/api/v1`;

// Per-endpoint timing, because one slow route hiding inside an aggregate is
// exactly what a load test is for.
const t = {
  reference: new Trend('ep_reference', true),
  subjectAll: new Trend('ep_subject_all', true),
  subjectAddr: new Trend('ep_subject_by_address', true),
  files: new Trend('ep_files_by_subject', true),
  posts: new Trend('ep_post_all', true),
  search: new Trend('ep_search', true),
};

export const options = {
  discardResponseBodies: false,
  stages: [
    { duration: '20s', target: 5 },
    { duration: '20s', target: 10 },
    { duration: '20s', target: 25 },
    { duration: '20s', target: 50 },
    { duration: '20s', target: 100 },
    { duration: '10s', target: 0 },
  ],
  thresholds: {
    // Abort rather than keep hammering something that has already fallen over.
    // A read that 5xxs is the service failing, not the test finding a limit.
    http_req_failed: [{ threshold: 'rate<0.02', abortOnFail: true, delayAbortEval: '10s' }],
    // Ten seconds is well past unusable; past this the run is only making the
    // outage longer.
    http_req_duration: [{ threshold: 'p(99)<10000', abortOnFail: true, delayAbortEval: '10s' }],
  },
};

// Addresses that actually hold material, so the paging and mapping work is real
// rather than an empty-result fast path. CSE and ECE are the loaded departments.
const ADDRESSES = [
  { departmentId: 1, yearId: 1, semesterId: 1 },
  { departmentId: 1, yearId: 2, semesterId: 2 },
  { departmentId: 2, yearId: 3, semesterId: 1 },
  { departmentId: 2, yearId: 4, semesterId: 2 },
];
const SUBJECT_IDS = [1, 2, 3, 114, 150, 200, 240];
const TERMS = ['unit 3', 'notes', 'question paper', 'lab', 'assignment'];

function pick(a) {
  return a[Math.floor(Math.random() * a.length)];
}

function get(url, trend, name) {
  const r = http.get(url, { tags: { name } });
  trend.add(r.timings.duration);
  check(r, { [`${name} 200`]: (x) => x.status === 200 });
  return r;
}

export default function () {
  // Weighted roughly like a reader: browse an address, open a subject, look at
  // its files. The reference lookups are what every page load does first.
  get(`${BASE}/department/all`, t.reference, 'reference');

  const a = pick(ADDRESSES);
  get(
    `${BASE}/subject/semester?departmentId=${a.departmentId}&yearId=${a.yearId}&semesterId=${a.semesterId}&size=50`,
    t.subjectAddr,
    'subject_by_address',
  );

  get(`${BASE}/file/subject?subjectId=${pick(SUBJECT_IDS)}&size=50`, t.files, 'files_by_subject');
  get(`${BASE}/subject/all?size=50`, t.subjectAll, 'subject_all');
  get(`${BASE}/post/all?size=20`, t.posts, 'post_all');
  get(`${BASE}/search/files?q=${encodeURIComponent(pick(TERMS))}&size=20`, t.search, 'search');
}
