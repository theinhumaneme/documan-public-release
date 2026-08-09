// How many requests per second the API serves *well*, as opposed to at all.
//
// The ramping test answers "where does it fall over". That is the less useful
// number: a service at its saturation point is one nobody wants to use, because
// throughput holds while latency goes to seconds. This drives a fixed arrival
// rate instead and reports what a reader would experience at each one, so the
// answer is a rate you would actually run at.
//
// constant-arrival-rate, not VUs: requests are offered on a schedule regardless
// of whether earlier ones have come back, which is how real traffic arrives. A
// VU-based test quietly throttles itself when the server slows down and reports
// a rosier picture than the server deserves.
//
//   k6 run -e BASE=http://127.0.0.1:8082 loadtest-rate.js
//
import http from 'k6/http';
import { check } from 'k6';

const BASE = `${__ENV.BASE || 'http://127.0.0.1:8082'}/api/v1`;

const RATES = [10, 20, 30, 45];
const STAGE = 20;

// One scenario per rate, started back to back, so each reports its own metrics
// rather than being averaged into one meaningless aggregate.
const scenarios = {};
RATES.forEach((rate, i) => {
  scenarios[`rate_${rate}`] = {
    executor: 'constant-arrival-rate',
    rate,
    timeUnit: '1s',
    duration: `${STAGE}s`,
    // Generous, because a starved VU pool would cap the arrival rate and the
    // test would measure itself rather than the server.
    preAllocatedVUs: Math.max(20, rate * 2),
    maxVUs: 200,
    startTime: `${i * (STAGE + 5)}s`,
    tags: { rate: String(rate) },
  };
});

export const options = {
  scenarios,
  thresholds: {
    http_req_failed: [{ threshold: 'rate<0.02', abortOnFail: true, delayAbortEval: '10s' }],
    http_req_duration: [{ threshold: 'p(99)<15000', abortOnFail: true, delayAbortEval: '10s' }],
    ...Object.fromEntries(RATES.map((r) => [`http_req_duration{rate:${r}}`, ['p(95)>=0']])),
  },
};

const ADDRESSES = [
  { departmentId: 1, yearId: 1, semesterId: 1 },
  { departmentId: 1, yearId: 2, semesterId: 2 },
  { departmentId: 2, yearId: 3, semesterId: 1 },
  { departmentId: 2, yearId: 4, semesterId: 2 },
];
const SUBJECT_IDS = [1, 2, 3, 114, 150, 200, 240];

function pick(a) {
  return a[Math.floor(Math.random() * a.length)];
}

// One request per iteration, not six. With a fixed arrival rate the iteration is
// the unit being scheduled, so a six-request iteration would mean the configured
// rate is six times the real one — and the reported number would be wrong in the
// flattering direction.
export default function () {
  const roll = Math.random();
  let r;
  if (roll < 0.35) {
    const a = pick(ADDRESSES);
    r = http.get(
      `${BASE}/subject/semester?departmentId=${a.departmentId}&yearId=${a.yearId}&semesterId=${a.semesterId}&size=50`,
      { tags: { name: 'subject_by_address' } },
    );
  } else if (roll < 0.7) {
    r = http.get(`${BASE}/file/subject?subjectId=${pick(SUBJECT_IDS)}&size=50`, {
      tags: { name: 'files_by_subject' },
    });
  } else if (roll < 0.85) {
    r = http.get(`${BASE}/department/all`, { tags: { name: 'reference' } });
  } else {
    r = http.get(`${BASE}/post/all?size=20`, { tags: { name: 'post_all' } });
  }
  check(r, { ok: (x) => x.status === 200 });
}
