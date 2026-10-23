"""Synthetic routes through the real UDP receiver; no routing or Fleet requests."""
import json
import copy
import socket
import sys
import time
import unittest
from unittest import mock

from openpilot.sunnypilot.mapd.korea.external_source import ExternalNavSource
from openpilot.sunnypilot.mapd.korea.route import RouteSource


POINTS = [[127., 37.5], [127.001, 37.501]]


class TestPhoneRoute(unittest.TestCase):
  def setUp(self):
    self.route = RouteSource()
    self.source = ExternalNavSource(port=0, route_source=self.route)
    self.source.start()
    self.addCleanup(self.source.stop)
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.settimeout(2)
    self.addCleanup(self.sock.close)
    self.seq = 0

  def control(self, **changes):
    self.seq += 1
    payload = {'session': 100, 'trip': 1, 'route_id': 1, 'seq': self.seq, 'destination_lat': 37.6,
                   'destination_lon': 127., 'speed_limit_kph': 50, 'items': []}
    payload.update(changes)
    self.sock.sendto(json.dumps(payload).encode(), ('127.0.0.1', self.source.port))
    return json.loads(self.sock.recv(8192))

  def piece(self, points=POINTS, **changes):
    payload = {'session': 100, 'trip': 1, 'route_id': 1,
                   'route': {'part': 0, 'parts': 1, 'count': len(points), 'points': points, 'layers': []}}
    payload.update(changes)
    self.sock.sendto(json.dumps(payload).encode(), ('127.0.0.1', self.source.port))

  def test_only_a_complete_route_is_applied_and_acknowledged(self):
    self.assertNotIn('route_ack', self.control())
    self.piece()
    self.assertEqual(self.control()['route_ack'], [100, 1, 1])
    self.assertEqual(self.route.latest(), ([(37.5, 127.), (37.501, 127.001)], []))
    self.assertEqual(self.source.latest().speed_limit_kph, 50)

  def test_no_trip_and_ended_trip_keep_safety_updates_alive(self):
    self.control(trip=0, route_id=0, destination_lat=0, destination_lon=0)
    self.control(trip=0, route_id=0, destination_lat=0, destination_lon=0, speed_limit_kph=60)
    self.assertEqual(self.source.latest().speed_limit_kph, 60)
    self.control()
    self.piece()
    self.control()
    self.route.set_position(37.6, 127.)
    self.control(speed_limit_kph=70)
    self.assertEqual(self.source.latest().speed_limit_kph, 70)
    self.assertIsNone(self.source.latest().destination)
    self.assertIsNone(self.route.applied())

  def test_end_arrival_and_ignition_off_cannot_be_revived_by_late_packets(self):
    for finish in ('zero', 'arrival', 'offroad'):
      with self.subTest(finish=finish):
        self.control(trip=self.seq + 1)
        trip = self.seq
        self.piece(trip=trip)
        self.assertIn('route_ack', self.control(trip=trip))
        if finish == 'zero':
          self.control(trip=trip, route_id=0, destination_lat=0, destination_lon=0)
        elif finish == 'arrival':
          self.route.set_position(37.6, 127.)
        else:
          self.route.set_started(True)
          self.route.set_started(False)
        self.piece(trip=trip)
        self.assertNotIn('route_ack', self.control(trip=trip))
        self.assertEqual(self.route.latest(), ([], []))

  def test_loss_reverse_order_duplicates_and_different_routes_do_not_mix(self):
    self.control()
    points = [[127., 37.5 + i * .00001] for i in range(130)]
    last = {'part': 1, 'parts': 2, 'count': 130, 'points': points[128:], 'layers': []}
    first = {'part': 0, 'parts': 2, 'count': 130, 'points': points[:128], 'layers': [[0, 3]]}
    self.piece(route=last)
    self.piece(route=last)
    self.piece(route=first, route_id=2)
    self.assertNotIn('route_ack', self.control())
    self.piece(route=first)
    self.assertEqual(self.control()['route_ack'], [100, 1, 1])
    good = self.route.latest()
    self.assertEqual(len(good[0]), 130)
    self.assertEqual(good[1], [good[0][:4]])
    self.control(route_id=2)
    self.piece(route=last, route_id=2)
    self.assertEqual(self.control(route_id=2)['route_ack'], [100, 1, 1])
    self.assertEqual(self.route.latest(), good)
    self.piece(route=first)  # delayed previous line
    self.assertEqual(self.route.latest(), good)
    self.piece(route=first, route_id=2)
    self.assertEqual(self.control(route_id=2)['route_ack'], [100, 1, 2])

  def test_a_route_number_cannot_be_replaced_by_different_duplicates(self):
    self.control()
    self.piece()
    self.control()
    self.piece(points=[[128., 36.], [128.1, 36.1]])
    self.control()
    self.assertEqual(self.route.latest()[0], [(37.5, 127.), (37.501, 127.001)])

  def test_invalid_shapes_counts_and_points_preserve_the_applied_route(self):
    self.control()
    self.piece()
    self.control()
    good = self.route.latest()
    self.control(route_id=2)
    valid = {'part': 0, 'parts': 1, 'count': 2, 'points': POINTS, 'layers': []}
    bad = [None, [], {**valid, 'count': True}, {**valid, 'count': 5001}, {**valid, 'count': 1},
           {**valid, 'parts': 5000}, {**valid, 'part': -1}, {**valid, 'part': True},
           {**valid, 'layers': [[0, 2]]}, {**valid, 'layers': [[True, 1]]},
           {**valid, 'layers': [[0, 1], [0, 1]]}]
    for value in (True, '127', None, float('nan'), float('inf'), 133, 10 ** 400):
      piece = copy.deepcopy(valid)
      piece['points'][0][0] = value
      bad.append(piece)
    for piece in bad:
      self.piece(route=piece, route_id=2)
      ack = self.control(route_id=2)
      self.assertEqual(ack['route_ack'], [100, 1, 1])
      self.assertEqual(self.route.latest(), good)
    self.piece(route_id=2)
    self.assertEqual(self.control(route_id=2)['route_ack'], [100, 1, 2])

  def test_new_trip_and_session_ignore_previous_control_packets(self):
    self.control()
    self.piece()
    self.control()
    self.assertNotIn('route_ack', self.control(trip=2))
    self.piece()
    self.control(trip=1)
    self.assertEqual(self.route.latest(), ([], []))
    self.piece(trip=2)
    self.assertEqual(self.control(trip=2)['route_ack'], [100, 2, 1])
    self.assertNotIn('route_ack', self.control(session=101, trip=1))
    self.control(session=100, trip=9)
    self.piece(session=100, trip=9)
    self.assertEqual(self.route.latest(), ([], []))

  def test_phone_disconnect_expires_items_but_keeps_the_completed_route(self):
    self.control()
    self.piece()
    self.control()
    good = self.route.latest()
    with mock.patch('openpilot.sunnypilot.mapd.korea.external_source.time.monotonic', return_value=time.monotonic() + 6):
      self.assertIsNone(self.source.latest())
      self.assertEqual(self.route.latest(), good)

  def test_5000_points_are_bounded_and_do_not_change_item_cadence(self):
    self.control()
    points = [[127., 37.5 + i * .000001] for i in range(5000)]
    for part in reversed(range(40)):
      self.piece(route={'part': part, 'parts': 40, 'count': 5000, 'points': points[part*128:(part+1)*128], 'layers': []})
      ack = self.control()
      self.assertEqual('route_ack' in ack, part == 0)
    self.assertEqual(len(self.route.latest()[0]), 5000)

  def test_every_answer_fits_and_oversized_input_does_not_kill_receiver(self):
    self.control()
    self.piece()
    self.control()
    for packet in (b'{"seq":7}', b'{"seq":700,"padding":"' + b'x' * 8200 + b'"}', b'[' * 1100):
      self.sock.sendto(packet, ('127.0.0.1', self.source.port))
      if packet == b'{"seq":7}':
        self.assertLessEqual(len(self.sock.recv(8192)), len(packet))
    self.assertEqual(self.control()['route_ack'], [100, 1, 1])
    self.source.car = lambda: (37.5, 127., '가' * 1000)
    self.seq += 1
    data = json.dumps({'seq': self.seq, 'padding': 'x' * 80}).encode()
    self.sock.sendto(data, ('127.0.0.1', self.source.port))
    ack = self.sock.recv(8192)
    self.assertLessEqual(len(ack), len(data))
    self.assertEqual(json.loads(ack)['route_ack'], [100, 1, 1])


def check_java_wire(lines):
  """Consume PhoneRoute.java --wire output through the production UDP boundary."""
  route = RouteSource()
  source = ExternalNavSource(port=0, route_source=route)
  source.start()
  try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
      sock.settimeout(2)
      sequence = 0
      for line in lines:
        packet = json.loads(line)
        data = line.strip().encode()
        assert len(data) <= 8192
        if 'route' in packet:
          sock.sendto(data, ('127.0.0.1', source.port))
        sequence += 1
        control = {k: packet[k] for k in ('session', 'trip', 'route_id')}
        control.update(seq=sequence, destination_lat=37.6, destination_lon=127.)
        data = json.dumps(control).encode()
        sock.sendto(data, ('127.0.0.1', source.port))
        answer = sock.recv(8192)
        assert len(answer) <= len(data)
      assert json.loads(answer)['route_ack'] == [100, 1, 1]
      points, spans = route.latest()
      assert len(points) == 5000 and points[0] == (37.5, 127.) and points[-1] == (37.504999, 127.)
      assert spans == [points[1:5], points[130:134]]
  finally:
    source.stop()
  print('Java -> UDP -> Python: 5000 points, longitude/latitude, layers and applied ACK passed')


if __name__ == '__main__':
  if sys.argv[1:] == ['--wire']:
    check_java_wire(sys.stdin)
  else:
    unittest.main()
