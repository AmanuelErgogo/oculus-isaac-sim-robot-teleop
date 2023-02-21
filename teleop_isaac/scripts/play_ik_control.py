#!/usr/bin/env python
# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES, ETH Zurich, and University of Toronto
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

"""
This script demonstrates how to use the inverse kinematics controller with the simulator.

The differential IK controller can be configured in different modes. It uses the Jacobians computed by
PhysX. This helps perform parallelized computation of the inverse kinematics.
"""

"""Launch Isaac Sim Simulator first."""


import argparse

from omni.isaac.kit import SimulationApp

# add argparse arguments
parser = argparse.ArgumentParser("Welcome to Orbit: Omniverse Robotics Environments!")
parser.add_argument("--headless", action="store_true", default=False, help="Force display off at all times.")
parser.add_argument("--robot", type=str, default="xarm7", help="Name of the robot.")
parser.add_argument("--num_envs", type=int, default=128, help="Number of environments to spawn.")
args_cli = parser.parse_args()

# launch omniverse app
config = {"headless": args_cli.headless}
simulation_app = SimulationApp(config)


"""Rest everything follows."""


import torch

import omni.isaac.core.utils.prims as prim_utils
from omni.isaac.cloner import GridCloner
from omni.isaac.core.simulation_context import SimulationContext
from omni.isaac.core.utils.carb import set_carb_setting
from omni.isaac.core.utils.viewports import set_camera_view

import omni.isaac.orbit.utils.kit as kit_utils
from omni.isaac.orbit.controllers.differential_inverse_kinematics import (
    DifferentialInverseKinematics,
    DifferentialInverseKinematicsCfg,
)
from omni.isaac.orbit.markers import StaticMarker
from omni.isaac.orbit.robots.config.franka import FRANKA_PANDA_ARM_WITH_PANDA_HAND_CFG
from omni.isaac.orbit.robots.config.universal_robots import UR10_CFG
from omni.isaac.orbit.robots.single_arm import SingleArmManipulator
from omni.isaac.orbit.utils.assets import ISAAC_NUCLEUS_DIR

from omni.isaac.orbit.robots.config.xarm import XARM_ARM_WITH_XARM_GRIPPER_CFG

"""
Main
"""
import rospy
from sensor_msgs.msg import JointState
from geometry_msgs.msg import Twist

global robot, sim, ee_goals, envs_positions, _eef_goals, follow, ik_commands
_eef_goals = []
follow = False
rospy.init_node("test_rosbridge", anonymous=True)


def joint_command_clb(msg):
    print("Joint command :", msg)


def cmd_vel_clb(msg):
    """ convert msg.angular to quaternion, and write to a global var ee_goals """
    global robot, sim, ee_goals, envs_positions, follow, ik_commands
    # ee_goals = []
    eef_pos = (robot.data.ee_state_w[:, 0:3] - envs_positions)[0][:].tolist()
    eef_orein = robot.data.ee_state_w[:, 3:7][0][:].tolist()
    eef_goals = [*eef_pos, *eef_orein]
    eef_goals[0] = eef_goals[0] + msg.linear.x * 0.05
    eef_goals[1] = eef_goals[0] + msg.linear.y * 0.05
    ee_goals = torch.tensor(eef_goals, device=sim.device)
    ik_commands[:] = ee_goals

    follow = True


def main():
    global robot, ee_goals, envs_positions, _eef_goals, follow, sim, ik_commands
    """Spawns a single-arm manipulator and applies commands through inverse kinematics control."""

    # Load kit helper
    sim = SimulationContext(
        stage_units_in_meters=1.0, physics_dt=0.01, rendering_dt=0.01, backend="torch", device="cuda:0"
    )
    # Set main camera
    set_camera_view([2.5, 2.5, 2.5], [0.0, 0.0, 0.0])
    # Enable GPU pipeline and flatcache
    if sim.get_physics_context().use_gpu_pipeline:
        sim.get_physics_context().enable_flatcache(True)
    # Enable hydra scene-graph instancing
    set_carb_setting(sim._settings, "/persistent/omnihydra/useSceneGraphInstancing", True)

    # Create interface to clone the scene
    cloner = GridCloner(spacing=2.0)
    cloner.define_base_env("/World/envs")
    # Everything under the namespace "/World/envs/env_0" will be cloned
    prim_utils.define_prim("/World/envs/env_0")

    # Spawn things into stage
    # Markers
    ee_marker = StaticMarker("/Visuals/ee_current", count=args_cli.num_envs, scale=(0.1, 0.1, 0.1))
    goal_marker = StaticMarker("/Visuals/ee_goal", count=args_cli.num_envs, scale=(0.1, 0.1, 0.1))
    # Ground-plane
    kit_utils.create_ground_plane("/World/defaultGroundPlane", z_position=-1.05)
    # Lights-1
    prim_utils.create_prim(
        "/World/Light/GreySphere",
        "SphereLight",
        translation=(4.5, 3.5, 10.0),
        attributes={"radius": 2.5, "intensity": 1000.0, "color": (0.75, 0.75, 0.75)},
    )
    # Lights-2
    prim_utils.create_prim(
        "/World/Light/WhiteSphere",
        "SphereLight",
        translation=(-4.5, 3.5, 10.0),
        attributes={"radius": 2.5, "intensity": 1000.0, "color": (1.0, 1.0, 1.0)},
    )
    # -- Table
    table_usd_path = f"{ISAAC_NUCLEUS_DIR}/Props/Mounts/SeattleLabTable/table_instanceable.usd"
    prim_utils.create_prim("/World/envs/env_0/Table", usd_path=table_usd_path)
    # -- Robot
    # resolve robot config from command-line arguments
    if args_cli.robot == "franka_panda":
        robot_cfg = FRANKA_PANDA_ARM_WITH_PANDA_HAND_CFG
    elif args_cli.robot == "ur10":
        robot_cfg = UR10_CFG
    elif args_cli.robot == "xarm7":
        robot_cfg = XARM_ARM_WITH_XARM_GRIPPER_CFG
    else:
        raise ValueError(f"Robot {args_cli.robot} is not supported. Valid: franka_panda, ur10, xarm7")
    # configure robot settings to use IK controller
    robot_cfg.data_info.enable_jacobian = True
    robot_cfg.rigid_props.disable_gravity = True
    # spawn robot
    robot = SingleArmManipulator(cfg=robot_cfg)
    robot.spawn("/World/envs/env_0/Robot", translation=(0.0, 0.0, 0.0))

    # Clone the scene
    num_envs = args_cli.num_envs
    envs_prim_paths = cloner.generate_paths("/World/envs/env", num_paths=num_envs)
    envs_positions = cloner.clone(source_prim_path="/World/envs/env_0", prim_paths=envs_prim_paths)
    # convert environment positions to torch tensor
    envs_positions = torch.tensor(envs_positions, dtype=torch.float, device=sim.device)
    # filter collisions within each environment instance
    physics_scene_path = sim.get_physics_context().prim_path
    cloner.filter_collisions(
        physics_scene_path, "/World/collisions", envs_prim_paths, global_paths=["/World/defaultGroundPlane"]
    )

    # Create controller
    # the controller takes as command type: {position/pose}_{abs/rel}
    ik_control_cfg = DifferentialInverseKinematicsCfg(
        command_type="pose_abs",
        ik_method="dls",
        position_offset=robot.cfg.ee_info.pos_offset,
        rotation_offset=robot.cfg.ee_info.rot_offset,
    )
    ik_controller = DifferentialInverseKinematics(ik_control_cfg, num_envs, sim.device)

    # Play the simulator
    sim.reset()
    # Acquire handles
    # Initialize handles
    robot.initialize("/World/envs/env_.*/Robot")
    # Reset states
    robot.reset_buffers()
    ik_controller.reset_idx()

    # Now we are ready!
    print("[INFO]: Setup complete...")

    # Create buffers to store actions
    ik_commands = torch.zeros(robot.count, ik_controller.num_actions, device=robot.device)
    robot_actions = torch.ones(robot.count, robot.num_actions, device=robot.device)

    # Track the given command
    # current_goal_idx = 0
    # ik_commands[:] = ee_goals[current_goal_idx]
    # ik_commands[:] = ee_goals

    # Define simulation stepping
    sim_dt = sim.get_physics_dt()
    # episode counter
    sim_time = 0.0
    count = 0
    # Note: We need to update buffers before the first step for the controller.
    robot.update_buffers(sim_dt)

    # DEBUG
    """
    print("eef_pose_w: ", robot.data.ee_state_w.shape)
    eef_pos = robot.data.ee_state_w[:, 0:3] - envs_positions
    eef_orein = robot.data.ee_state_w[:, 3:7]
    _eef_goals = eef_pos[0][:].tolist()
    _eef_goals.append(eef_orein[0][:].tolist())
    print("eef_goals: ", _eef_goals)
    """

    # Simulate physics
    # Simulate physics
    while simulation_app.is_running():
        # subscribers
        # rospy.Subscriber("/joint_command", JointState, joint_command_clb)
        # rospy.Subscriber("isaac/cmd_vel", Twist, cmd_vel_clb)
        # If simulation is stopped, then exit.
        if sim.is_stopped():
            break
        # If simulation is paused, then skip.
        if not sim.is_playing():
            sim.step(render=not args_cli.headless)
            continue
        # set the controller commands
        if follow is True:
            ik_controller.set_command(ik_commands)
            # compute the joint commands
            robot_actions[:, : robot.arm_num_dof] = ik_controller.compute(
                robot.data.ee_state_w[:, 0:3] - envs_positions,
                robot.data.ee_state_w[:, 3:7],
                robot.data.ee_jacobian,
                robot.data.arm_dof_pos,
            )
            # in some cases the zero action correspond to offset in actuators
            # so we need to subtract these over here so that they can be added later on
            arm_command_offset = robot.data.actuator_pos_offset[:, : robot.arm_num_dof]
            # offset actuator command with position offsets
            # note: valid only when doing position control of the robot
            robot_actions[:, : robot.arm_num_dof] -= arm_command_offset
            # apply actions
            robot.apply_action(robot_actions)

            follow = False
        # perform step
        sim.step()
        # update sim-time
        sim_time += sim_dt
        count += 1
        # note: to deal with timeline events such as stopping, we need to check if the simulation is playing
        if sim.is_playing():
            # update buffers
            robot.update_buffers(sim_dt)
            rospy.Subscriber("/joint_command", JointState, joint_command_clb)
            rospy.Subscriber("isaac/cmd_vel", Twist, cmd_vel_clb)
            # update marker positions
            ee_marker.set_world_poses(robot.data.ee_state_w[:, 0:3], robot.data.ee_state_w[:, 3:7])
            goal_marker.set_world_poses(ik_commands[:, 0:3] + envs_positions, ik_commands[:, 3:7])


if __name__ == "__main__":
    # Run IK example with Manipulator
    main()
    # Close the simulator
    simulation_app.close()
