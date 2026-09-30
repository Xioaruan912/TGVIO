import assert from 'node:assert/strict';
import test from 'node:test';
import { SelectedPlaylist } from '../.test-dist/library-playlist.js';
test('only selected items are played, preserving selection order without duplicates', () => {
  const q=new SelectedPlaylist([{id:'b'},{id:'a'},{id:'b'}]);
  assert.deepEqual(q.items.map(x=>x.id),['b','a']);
  assert.equal(q.current.id,'b');assert.equal(q.hasPrevious,false);
  assert.equal(q.move(1),true);assert.equal(q.current.id,'a');assert.equal(q.hasNext,false);
});
test('ended callback cannot advance twice or from an old player', () => {
  const q=new SelectedPlaylist([{id:'a'},{id:'b'},{id:'c'}]);const old=q.token;
  assert.equal(q.ended(old),'next');assert.equal(q.current.id,'b');
  assert.equal(q.ended(old),'stale');assert.equal(q.current.id,'b');
  const b=q.token;q.move(1);assert.equal(q.ended(b),'stale');
  assert.equal(q.ended(q.token),'finished');assert.equal(q.current.id,'c');
});
test('destroy rejects late events and traversal is bounded', () => {
  const q=new SelectedPlaylist([{id:'a'}]);assert.equal(q.move(-1),false);assert.equal(q.move(1),false);
  const token=q.token;q.destroy();assert.equal(q.ended(token),'stale');assert.equal(q.current,null);
  assert.equal(new SelectedPlaylist(Array.from({length:900},(_,i)=>({id:String(i)}))).items.length,100);
});
