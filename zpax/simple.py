import json

from zpax import tzmq
from paxos import multi, basic
from paxos.leaders import heartbeat

from twisted.internet import defer, task, reactor


class SimpleHeartbeatProposer (heartbeat.Proposer):
    hb_period       = 0.5
    liveness_window = 1.5

    def __init__(self, simple_node):
        self.node = simple_node

        super(SimpleHeartbeatProposer, self).__init__(self.node.node_uid,
                                                      self.node.paxos_threshold,
                                                      leader_uid = self.node.current_leader)

    def send_prepare(self, proposal_id):
        self.node.paxos_send_prepare(proposal_id)

    def send_accept(self, proposal_id, proposal_value):
        self.node.paxos_send_accept(proposal_id, proposal_value)

    def send_heartbeat(self, leader_proposal_id):
        self.node.paxos_send_heartbeat(leader_proposal_id)

    def schedule(self, msec_delay, func_obj):
        pass

    def on_leadership_acquired(self):
        self.node.paxos_on_leadership_acquired()

    def on_leadership_lost(self):
        self.node.paxos_on_leadership_lost()

    def on_leadership_change(self, prev_leader_uid, new_leader_uid):
        self.node.paxos_on_leadership_change(prev_leader_uid, new_leader_uid)
        


class SimpleMultiPaxos(multi.MultiPaxos):

    def __init__(self, simple_node):
        self.simple_node  = simple_node
        self.node_factory = simple_node._node_factory
        
        super(SimpleMultiPaxos, self).__init__( simple_node.node_uid,
                                                simple_node.paxos_threshold,
                                                simple_node.sequence_number )
        

        
    def on_proposal_resolution(self, instance_num, value):
        self.simple_node.onProposalResolution(instance_num, value)
    



class SimpleNode (object):

    def __init__(self, node_uid,
                 local_pub_sub_addr,   local_rtr_addr,
                 remote_pub_sub_addrs,
                 paxos_threshold,
                 initial_value='', sequence_number=0):

        self.node_uid         = node_uid
        self.local_ps_addr    = local_pub_sub_addr
        self.local_rtr_addr   = local_rtr_addr
        self.remote_ps_addrs  = remote_pub_sub_addrs
        self.paxos_threshold  = paxos_threshold
        self.value            = initial_value
        self.sequence_number  = sequence_number
        self.current_leader   = None

        self.waiting_clients  = set() # Contains router addresses of clients waiting for updates

        self.mpax             = SimpleMultiPaxos(self)

        self.heartbeat_poller = task.LoopingCall( self._poll_heartbeat         )
        self.heartbeat_pulser = task.LoopingCall( self._pulse_leader_heartbeat )
        
        self.pub              = tzmq.ZmqPubSocket()
        self.sub              = tzmq.ZmqSubSocket()
        self.router           = tzmq.ZmqRouterSocket()

        self.sub.subscribe = 'zpax'
        
        self.sub.messageReceived    = self.onSubReceived
        self.router.messageReceived = self.onRouterReceived
        
        self.pub.bind(self.local_ps_addr)
        self.router.bind(self.local_rtr_addr)

        for x in remote_pub_sub_addrs:
            print 'Connecting: ', x
            self.sub.connect(x)

        self.heartbeat_poller.start( SimpleHeartbeatProposer.liveness_window )


    def _node_factory(self, node_uid, quorum_size, resolution_callback):
        return basic.Node( SimpleHeartbeatProposer(self),
                           basic.Acceptor(),
                           basic.Learner(quorum_size),
                           resolution_callback )


    def _poll_heartbeat(self):
        self.mpax.node.proposer.poll_liveness()

        
    def _pulse_leader_heartbeat(self):
        self.mpax.node.proposer.pulse()

        
    def paxos_on_leadership_acquired(self):
        print self.node_uid, 'I have the leader!'
        self.heartbeat_pulser.start( SimpleHeartbeatProposer.hb_period )

        
    def paxos_on_leadership_lost(self):
        print self.node_uid, 'I LOST the leader!'
        if self.heartbeat_pulser.running:
            self.heartbeat_pulser.stop()


    def paxos_on_leadership_change(self, prev_leader_uid, new_leader_uid):
        print '*** Change of guard: ', prev_leader_uid, new_leader_uid


    def paxos_send_prepare(self, proposal_id):
        #print self.node_uid, 'sending prepare: ', proposal_id
        self.publish( dict( type='paxos_prepare', sequence_number=self.sequence_number, node_uid=self.node_uid ),
                      [proposal_id,] )

        
    def paxos_send_accept(self, proposal_id, proposal_value):
        self.publish( dict( type='paxos_accept', sequence_number=self.sequence_number, node_uid=self.node_uid ),
                      [proposal_id, proposal_value] )

        
    def paxos_send_heartbeat(self, leader_proposal_id):
        self.publish( dict( type='paxos_heartbeat', sequence_number=self.sequence_number, node_uid=self.node_uid ),
                      [leader_proposal_id,] )


    def publish(self, *parts):
        jparts = [ json.dumps(p) for p in parts ]
        self.pub.send( 'zpax', *jparts )
        self.onSubReceived(['zpax'] + jparts)


    def onSubReceived(self, msg_parts):
        '''
        msg_parts - [0] 'zpax'
                    [1] is SimpleNode's JSON-encoded structure
                    [2] If present, it's a JSON-encoded Paxos message
        '''
        try:
            parts = [ json.loads(p) for p in msg_parts[1:] ]
        except ValueError:
            print 'Invalid JSON: ', msg_parts
            return

        if not 'type' in parts[0]:
            print 'Missing message type'
            return

        fobj = getattr(self, '_on_sub_' + parts[0]['type'], None)
        
        if fobj:
            fobj(*parts)



    def _check_sequence(self, header):
        if header['sequence_number'] > self.sequence_number:
            self.publish( dict( type='get_value' ) )

        if header['sequence_number'] < self.sequence_number:
            self.publish_value()


    def _on_sub_paxos_heartbeat(self, header, pax):
        self.mpax.node.proposer.recv_heartbeat( tuple(pax[0]) )

    
    def _on_sub_paxos_prepare(self, header, pax):
        #print self.node_uid, 'got prepare', header, pax
        self._check_sequence(header)
        r = self.mpax.recv_prepare(self.sequence_number, tuple(pax[0]))
        if r:
            #print self.node_uid, 'sending promise'
            self.publish( dict( type='paxos_promise', sequence_number=self.sequence_number, node_uid=self.node_uid ),
                          r )

            
    def _on_sub_paxos_promise(self, header, pax):
        #print self.node_uid, 'got promise', header, pax
        self._check_sequence(header)
        r = self.mpax.recv_promise(self.sequence_number, header['node_uid'],
                                   tuple(pax[0]), tuple(pax[1]) if pax[1] else None, pax[2])
        if r and r[1] is not None:
            print self.node_uid, 'sending accept', r
            self.paxos_send_accept( *r )
            

    def _on_sub_paxos_accept(self, header, pax):
        self._check_sequence(header)
        self.mpax.recv_accept_request(self.sequence_number, tuple(pax[0]), pax[1])

        
    def _on_sub_get_value(self, header):
        self.publish_value()
        

    def _on_sub_value(self, header):
        if header['sequence_number'] > self.sequence_number:
            self.value           = header['value']
            self.sequence_number = header['sequence_number']
            if self.mpax.node.proposer.leader:
                self.paxos_on_leadership_lost()
            self.mpax.set_instance_number(self.sequence_number)

            
    def _on_sub_value_proposal(self, header):
        if header['sequence_number'] == self.sequence_number:
            self.mpax.set_proposal(self.sequence_number, header['value'])

            
    def publish_value(self):
        self.publish( dict(type='value', sequence_number=self.sequence_number, value=self.value) )


    def onProposalResolution(self, instance_num, value):
        self.value            = value
        self.sequence_number  = instance_num + 1

        for addr in self.waiting_clients:
            self.reply_value(addr)

        self.waiting_clients.clear()


    def onRouterReceived(self, msg_parts):
        try:
            addr  = msg_parts[0]
            parts = [ json.loads(p) for p in msg_parts[1:] ]
        except ValueError:
            print 'Invalid JSON: ', msg_parts

        if parts or not 'type' in parts[0]:
            print 'Missing message type'
            return

        fobj = getattr(self, '_on_router_' + parts[0]['type'], None)
        
        if fobj:
            fobj(addr, *parts)

            
    def reply(self, addr, *parts):
        jparts = [ json.dumps(p) for p in parts ]
        self.router.send( addr, *jparts )

        
    def reply_value(self, addr):
        self.reply(addr, dict(sequence_number=self.sequence_number-1, value=self.value) )

        
    def _on_router_propose_value(self, addr, header):
        if header['sequence_number'] == self.sequence_number:
            self.publish( dict(type='value_proposal', sequence_number=self.sequence_number, value=header['value']) )
            self.mpax.set_proposal(self.sequence_number, header['value'])
            self.reply(addr, dict(proposed=True))
        else:
            self.reply(addr, dict(proposed=False, message='Invalid sequence number'))

            
    def _on_router_query_value(self, addr, header):
        self.reply_value(addr)

        
    def _on_router_get_next_value(self, addr, header):
        if header['sequence_number'] < self.sequence_number:
            self.reply_value(addr)
        else:
            self.waiting_clients.add( addr )
        

        
        



        
        