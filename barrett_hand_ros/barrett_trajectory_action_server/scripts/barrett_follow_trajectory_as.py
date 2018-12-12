#! /usr/bin/python
import sensor_msgs.msg
import rospy
import threading
import numpy as np
import std_srvs.srv
import barrett_trajectory_action_server.srv
import barrett_trajectory_action_server.msg

from actionlib import SimpleActionServer

from control_msgs.msg import (FollowJointTrajectoryAction,
                              FollowJointTrajectoryFeedback,
                              FollowJointTrajectoryResult)

from barrett_tactile_msgs.msg import TactileInfo
from bhand_controller.srv import Actions, SetControlMode
from bhand_controller.msg import State, TactileArray, Service
from barrett_tactile_msgs.msg import TactileInfo

from collections import namedtuple, defaultdict

import tactile_maps

# Named Tuple reprenting the dof state of the hand
DOF_STATE = namedtuple("DOF", ["spread", "f1", "f2", "f3"])

# INDICES FOR DOFS TO BE SENT TO THE HAND TO EXECUTE
SPREAD_DOF_INDEX = 0
F1_DOF_INDEX = 2
F2_DOF_INDEX = 3
F3_DOF_INDEX = 1

# INDICES for DOFS from waypoints provided by MOVEIt!
SPREAD_WAYPOINT_INDEX = 0
F1_WAYPOINT_INDEX = 1
F2_WAYPOINT_INDEX = 2
F3_WAYPOINT_INDEX = 3

def waypoint_to_np(wp):
    return np.array([
        wp.positions[SPREAD_WAYPOINT_INDEX],  # spread
        wp.positions[F3_WAYPOINT_INDEX],  # f3
        wp.positions[F1_WAYPOINT_INDEX],  # f1
        wp.positions[F2_WAYPOINT_INDEX],  # f2
    ])


def dof_state_to_np(dof):
    return np.array([dof.spread, dof.f3, dof.f1, dof.f2])


def dof_state_from_np(dof_np):
    return DOF_STATE(
        spread=dof_np[SPREAD_DOF_INDEX],
        f1=dof_np[F1_DOF_INDEX],
        f2=dof_np[F2_DOF_INDEX],
        f3=dof_np[F3_DOF_INDEX])


class JointTracjectoryActionServer(object):
    def __init__(self):

        # This action server takes trajectories generated by moveit
        # and executes them using a P-controller
        self.action_server = SimpleActionServer(
            "/barrett/follow_joint_trajectory",
            FollowJointTrajectoryAction,
            execute_cb=self._follow_joint_trajectory_cb,
            auto_start=False)

        # This creates a proxy for the action server for barrett hand
        # Sends INIT action to the server
        self.bhand_node_name = "bhand_node"
        self._actions_service_name = '/%s/actions'%self.bhand_node_name
        rospy.wait_for_service(self._actions_service_name)
        self._service_bhand_actions = rospy.ServiceProxy(self._actions_service_name, Actions)
        self.send_bhand_action(Service.INIT_HAND)

        self._f1_offsets   = np.zeros(24)
        self._f2_offsets   = np.zeros(24)
        self._f3_offsets   = np.zeros(24)
        self._palm_offsets = np.zeros(24)

        #COMMENT
        self._tact_data = TactileArray()

        # This subscribers listens for the raw tactile information 
        # from the hand
        self._tact_topic = '/%s/tact_array'%self.bhand_node_name
        self._tactile_sub = rospy.Subscriber(self._tact_topic,
                                             TactileArray,
                                             self._receive_tact_data)

        #This subscriber gets the current joint states of the hand
        self._joint_sub = rospy.Subscriber("/joint_states",
                                           sensor_msgs.msg.JointState,
                                           self.update_joint_state_cb)

        # This publisher sends commands to the hand
        # We currently operate in the velocity control mode
        self.hand_cmd_pub = rospy.Publisher(
            "/bhand_node/command", sensor_msgs.msg.JointState, queue_size=10)

        # ServiceProxy for setting the control mode. 
        # We set the control mode to velocity at the start
        # of each call to _follow_joint_trajectory_cb
        self.service_set_mode = rospy.ServiceProxy(
            '/bhand_node/set_control_mode', SetControlMode)

        # This service allows clients to reset our tactile state
        # we keep track of which fingers have made contact with 
        # the object and stop those fingers from receiving 
        # non-zero velocity commands until the tactile state is reset
        self.service_reset_tactile_state = rospy.Service(
            '/barrett/reset_tactile_state', std_srvs.srv.Empty,
            self.reset_tactile_state)

        # This allows a client to set whether or not the hand should ignore
        # Tactile contacts during trajectory execution
        self.service_set_ignore_tactile_state = rospy.Service(
            '/barrett/set_ignore_tactile_state', std_srvs.srv.SetBool,
            self.set_ignore_tactile_state)

        # This allows a client to get the frames of reference
        # for tactile contacts which have stopped hand movement.
        # self.service_get_tactile_info = rospy.Service(
        #     '/barrett/get_tactile_info',
        #     barrett_trajectory_action_server.srv.GetTactileContacts,
        #     self.get_tactile_info)

        self.publisher_get_tactile_info = rospy.Publisher(
            '/barrett/get_tactile_info',
            barrett_trajectory_action_server.msg.TactileContactFrames,
            queue_size=10
        )

        #This mutex locks the current joint and dof state
        self.current_joint_and_dof_state_mutex = threading.Lock()

        #This mutex locks the current tactile state
        self.current_tactile_state_mutex = threading.Lock()

        # This is the last recorded joint state for the hand
        self.current_joint_state = None

        # This is the last recorded dof state of the hand. These values 
        # are used to determine new velocity commands 
        # in the follow_joint_trajectory_cb
        self.current_dof = None
        # This is the last recorded tactile state of the hand 
        self.current_tactile_state = None

        # This is the feedback published as the hand moves along a trajectory.
        self.feedback = FollowJointTrajectoryFeedback()
        self.feedback.joint_names = [
            'bh_j11_joint', 
            'bh_j32_joint', 
            'bh_j12_joint', 
            'bh_j22_joint'
        ]

        self.result = FollowJointTrajectoryResult()

        # By default we do not ignore tactile state.
        # This means that if a tactile sensor registers contact
        # beyond a threshold, that dof will no longer move until the 
        # tactile state if reset
        self.ignore_tactile_state = True

        # This is used for computing the new dof velocities.
        # any dof that has had a tactile contact will have 
        # its activated_dofs value set to 0.  This masks out any
        # velocity commands for that dof so that it does not move 
        self.activated_dofs = np.ones(4)

        # the unique set of frame names for activated tactile contacts
        self.tactile_info = set()


        # This is the absolute tolerance for our dofs as we execute a trajectory.
        # If at any point in execution any true dof value is not within this threshold 
        # of the desired dof value, then the action server is aborted.
        self.EXECUTION_WAYPOINT_THRESHOLD = 2.0

        # this is the absolute tolerance for our dofs at the start of trajectory execution. 
        # this ensure before we begin executing a trajectory that the hand is actually in the 
        # position specified at the first waypoint of the trajectory
        self.START_POINT_THRESHOLD = 0.1

        #COMMENTS
        self.TACTILE_THRESHOLD = 0.7

        # This in HZ is how often we send a command to the hand from within
        # the follow trajectory callback
        self.control_rate = 10  #Hz

        # actually start the action server to listen for new trajectories to follow.
        self.action_server.start()

        loop = rospy.Rate(10)
        while not rospy.is_shutdown():
            response = barrett_trajectory_action_server.msg.TactileContactFrames(tactile_frames=list(self.tactile_info))

            self.publisher_get_tactile_info.publish(response)
            loop.sleep()

    def send_bhand_action(self, action):    
        '''
            Calls the service to set the control mode of the hand
            @param action: Action number (defined in msgs/Service.msg)
            @type action: int
        '''         
        try:
            ret = self._service_bhand_actions(action)               
        except ValueError, e:
            rospy.logerr('BHandGUI::send_bhand_action: (%s)'%e)
        except rospy.ServiceException, e:
            rospy.logerr('BHandGUI::send_bhand_action: (%s)'%e)

    def _receive_tact_data(self, msg):
        if self.ignore_tactile_state:
            # rospy.loginfo("Ignoring tactile info")
            return
            
        self._tact_data = msg

        finger1_array = np.array(msg.finger1)
        finger2_array = np.array(msg.finger2)
        finger3_array = np.array(msg.finger3)
        # palm_array = np.array(msg.palm)

        # First several receive messages are useless
        if len(finger1_array) == 0:
            return

        finger1_nonzero = np.where((finger1_array - self._f1_offsets) > self.TACTILE_THRESHOLD)[0]
        finger2_nonzero = np.where((finger2_array - self._f2_offsets) > self.TACTILE_THRESHOLD)[0]
        finger3_nonzero = np.where((finger3_array - self._f3_offsets) > self.TACTILE_THRESHOLD)[0]
        # palm_nonzero = np.where((palm_array - self._palm_offsets) > self.TACTILE_THRESHOLD)

        map(lambda frame_id: self.tactile_info.add(tactile_maps.tact_to_finger1_map[frame_id]), finger1_nonzero)
        map(lambda frame_id: self.tactile_info.add(tactile_maps.tact_to_finger2_map[frame_id]), finger2_nonzero)
        map(lambda frame_id: self.tactile_info.add(tactile_maps.tact_to_finger3_map[frame_id]), finger3_nonzero)
        # map(lamba frame_id: self.tactile_info.add(tactile_maps.tact_to_palm_map[frame_id]), np.where(palm_array > self.TACTILE_THRESHOLD))

        if len(finger1_nonzero) > 0:
            self.activated_dofs[F1_DOF_INDEX] = 0
        if len(finger2_nonzero) > 0:
            self.activated_dofs[F2_DOF_INDEX] = 0
        if len(finger3_nonzero) > 0:
            self.activated_dofs[F3_DOF_INDEX] = 0

    def _follow_joint_trajectory_cb(self, goal):

        # Ensure that the hand is in a velocity control mode
        # rather than a position control mode
        self.service_set_mode('VELOCITY')

        start_waypoint_np = waypoint_to_np(goal.trajectory.points[0])
        current_dof_np = dof_state_to_np(self.current_dof)

        # first check that we are close to the starting point
        if not np.allclose(
                start_waypoint_np, current_dof_np,
                atol=self.START_POINT_THRESHOLD):
            rospy.logerr(
                "CANNOT EXECUTE TRAJECTORY: our current dof values: %s, are far from the trajectory starting dof values: "
                % (current_dof_np, start_waypoint_np))
            return

        start_time = rospy.Time.now()
        rate = rospy.Rate(self.control_rate)
        success = True

        for i, waypoint in enumerate(goal.trajectory.points):

            tactile_info_msg = barrett_trajectory_action_server.msg.TactileContactFrames(tactile_frames=list(self.tactile_info))
            self.publisher_get_tactile_info.publish(tactile_info_msg)

            # first make sure we have not deviated to far from trajectory
            # abort if so
            current_dof_np = dof_state_to_np(self.current_dof)
            waypoint_np = waypoint_to_np(waypoint)

            gain = np.array([0.4, 0.4, 0.4, 0.4])
            current_position_error = current_dof_np - waypoint_np
            if not np.allclose(
                    waypoint_np * self.activated_dofs,
                    current_dof_np * self.activated_dofs,
                    atol=self.EXECUTION_WAYPOINT_THRESHOLD):
                rospy.logerr(
                    "STOPPING EXECUTE TRAJECTORY: our current dof values: %s, are far from the expected current dof values: %s"
                    % (str(current_dof_np), str(waypoint_np)))
                self.action_server.set_aborted()
                return

            velocity = np.clip(-gain * current_position_error, -0.1, 0.1)
            # second still within the time allocated to this waypoint
            # continue to send waypoint velocity command
            cmd_msg = sensor_msgs.msg.JointState()
            cmd_msg.name = [
                'bh_j11_joint', 'bh_j32_joint', 'bh_j12_joint', 'bh_j22_joint'
            ]

            # [self.base_spread, self.finger3_spread, self.finger1_spread, self.finger2_spread]
            cmd_msg.position = [0, 0, 0, 0]
            cmd_msg.velocity = velocity
            cmd_msg.effort = [0, 0, 0, 0]

            self.feedback.desired = waypoint
            self.feedback.actual.positions = self.current_joint_state
            self.feedback.actual.velocities = self.current_joint_state
            self.feedback.actual.accelerations = self.current_joint_state
            self.feedback.actual.time_from_start = waypoint.time_from_start

            while waypoint.time_from_start >= rospy.Time.now() - start_time:

                current_dof_np = dof_state_to_np(self.current_dof)
                waypoint_np = waypoint_to_np(waypoint)
                current_position_error = current_dof_np - waypoint_np
                
                # clip the range of the output velocity
                velocity = np.clip(-gain * current_position_error, -0.4,  0.4)
                
                # mask the output velocity, only the activated dofs will move.
                # the other dofs, disabled due to tactile contact will have their
                # desired velocity set to 0.
                velocity *= self.activated_dofs

                cmd_msg.velocity = velocity
                
                # determine our tolerance for being finished with the waypoint.
                # we have a much higher tolerance for the final waypoint in the
                # trajectory
                is_last_waypoint = (i == len(goal.trajectory.points) - 1)
                if is_last_waypoint:
                    tolerance = 0.03
                else:
                    tolerance = 0.15

                rospy.logdebug(
                    "Position {} Velocity {}, tolerance {}".format(current_dof_np, velocity, tolerance))

                # if we are within our tolerance of the current
                # waypoint, then we can break out of the while loop
                # and move onto the next waypoint
                if np.allclose(
                        current_position_error * self.activated_dofs,
                        np.zeros_like(current_position_error),
                        atol=tolerance):
                    break

                # check if something external has told us to stop
                # (Ctrl C) or a prempt
                if self.action_server.is_preempt_requested(
                ) or rospy.is_shutdown():
                    self.action_server.set_preempted()
                    rospy.logerr("PREEMPTING HAND FOLLOW TRAJECTORY")
                    return

                # Everything is going smoothly, send velocity cmd to hand
                self.hand_cmd_pub.publish(cmd_msg)

                rate.sleep()

        if success:
            rospy.loginfo('Barret Hand Follow Joint Trajectory Succeeded')
            self.result.error_code = 0  # 0 means Success
            self.action_server.set_succeeded(self.result)

    def update_joint_state_cb(self, msg):
        # hand message not arm message
        if msg.name[0] == 'bh_j23_joint':
            self.current_joint_and_dof_state_mutex.acquire()
            self.current_joint_state = msg
            self.current_dof = DOF_STATE(
                spread=msg.position[6],
                f1=msg.position[1],
                f2=msg.position[2],
                f3=msg.position[3])
            self.current_joint_and_dof_state_mutex.release()

    def reset_tactile_state(self, req):
        rospy.loginfo("Resetting tactile state")

        self.activated_dofs = np.ones(4)
        self.tactile_info = set()

        #REZERO
        self._f1_offsets = np.array(self._tact_data.finger1)
        self._f2_offsets = np.array(self._tact_data.finger2)
        self._f3_offsets = np.array(self._tact_data.finger3)
        self._palm_offsets = np.array(self._tact_data.palm)

        rospy.loginfo("Finished resetting tactile state")
        return std_srvs.srv.EmptyResponse()
        #return ()

    def set_ignore_tactile_state(self, ignore_flag):
        rospy.loginfo("Setting ignore tactile to {}".format(ignore_flag))
        self.ignore_tactile_state = ignore_flag
        return std_srvs.srv.SetBoolResponse(
            success=True, message="Flag set correctly")


if __name__ == "__main__":
    rospy.init_node("Barrett_Trajectory_Follower", log_level=rospy.DEBUG)
    joint_trajectory_follower = JointTracjectoryActionServer()
    rospy.spin()
