const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const yaml = fs.readFileSync(path.join(__dirname, '../common/everything-presence-pro-base.yaml'), 'utf8');
// Execute the actual raw filter and expiry bodies with lightweight ESPHome mocks.
function body(start, end) {
  return yaml.split(start)[1].split(end)[0]
    .replace(/const (bool|uint32_t) /g, 'const ')
    .replace(/uint32_t\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g, '(($1) >>> 0)')
    .replace(/id\((\w+)\)/g, 's.$1');
}
const raw = new Function('s', 'millis', 'x', body('      # Observe raw edges before delayed_off merges public Motion transitions.\n      - lambda: |-\n', '      - delayed_off:'));
const expire = new Function('s', 'millis', body('          // Half-open window; unsigned subtraction handles millis rollover.\n', '\nbinary_sensor:'));
function fixture(count = 2) {
  const s = {relay_pir_count: 0, relay_pir_started: 0, relay_pir_high: false,
    relay_pir_qualified: false, relay_motion_count: {state: count},
    relay_motion_window: {state: 1}, system_alarm_mode: {current_option: () => 'Motion Only'},
    update_relay_state: {execute() { s.updates++; }}, updates: 0};
  return {s, edge: (x,t) => raw(s, () => t, x), tick: t => expire(s, () => t)};
}
test('distinct edges qualify even while public Motion remains high; prolonged high does not', () => {
  const {s,edge} = fixture(); edge(true,0); edge(true,100); assert.equal(s.relay_pir_count,1);
  edge(false,200); edge(true,999); assert.equal(s.relay_pir_qualified,true); assert.equal(s.updates,1);
});
test('exact boundary starts a fresh window and incomplete sequences expire', () => {
  const {s,edge,tick} = fixture(); edge(true,0); edge(false,1); edge(true,1000);
  assert.equal(s.relay_pir_qualified,false); assert.equal(s.relay_pir_count,1);
  tick(2000); assert.equal(s.relay_pir_count,0);
});
test('window and expiry work across millis rollover', () => {
  const {s,edge,tick} = fixture(); edge(true,0xffffff00); edge(false,0xffffff01);
  edge(true,100); assert.equal(s.relay_pir_qualified,true);
  const f=fixture(); f.edge(true,0xffffff00); f.tick(744); assert.equal(f.s.relay_pir_count,0);
});
test('count one qualifies first observed high; qualification survives counting window', () => {
  const {s,edge,tick}=fixture(1); edge(true,0); tick(60000); assert.equal(s.relay_pir_qualified,true);
});
test('disabled raw events preserve manual relay output', () => {
  const {s,edge}=fixture(1); s.system_alarm_mode.current_option=()=> 'Disabled';
  edge(true,0); assert.equal(s.updates,0);
});
test('configuration reset retains raw level and requires a new edge', () => {
  const {s,edge}=fixture(); edge(true,0);
  const reset=body('  - id: reset_relay_motion\n    then:\n      - lambda: |-\n','      - if:');
  new Function('s',reset)(s); edge(true,1); assert.equal(s.relay_pir_count,0);
  edge(false,2); edge(true,3); assert.equal(s.relay_pir_count,1);
});
const relay = new Function('s', body('  - id: update_relay_state\n    mode: restart\n    then:\n      - lambda: |-\n', '  - id: control_leds').replace(/\b(auto|bool) /g,'let '));
test('relay mode truth table, count-one compatibility and NO/NC mapping', () => {
  for (const count of [1,2]) for (const motion of [false,true])
    for (const presence of [false,true]) for (const nc of [false,true]) {
      const {s}=fixture(count); s.pir_motion={state:motion}; s.occupancy={state:motion||presence};
      s.mmwave_occupancy={state:presence}; s.relay_pir_qualified=false;
      s.relay_contact_mode={current_option:()=>nc?'Normally Closed (NC)':'Normally Open (NO)'};
      let output; s.system_alarm_relay={turn_on(){output=true},turn_off(){output=false}};
      const qualified=count===1&&motion;
      for (const [mode,active] of [['Motion Only',qualified],['Presence Only',motion||presence],
        ['Motion or Presence',qualified||presence],['Motion and Presence',qualified&&presence],['Disabled',false]]) {
        s.system_alarm_mode.current_option=()=>mode; relay(s); assert.equal(output,nc?!active:active,mode);
      }
      s.relay_pir_qualified=true;
      s.system_alarm_mode.current_option=()=> 'Motion and Presence'; relay(s);
      const active=(count===1?motion:true)&&presence;
      assert.equal(output,nc?!active:active);
    }
});
