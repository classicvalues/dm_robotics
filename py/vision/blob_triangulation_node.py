# Copyright 2020 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module defining a ROS node to triangulate the barycenter of colored blobs."""

import collections
import itertools
from typing import Collection, Mapping, Optional, Tuple

from absl import logging
from dmr_vision import ros_utils
from dmr_vision import triangulation as linear_triangulation
from dmr_vision import types
from dmr_vision import utils
import numpy as np
import rospy


class BlobTriangulationNode:
  """A ROS node for triangulating prop positions in a robot base frame."""

  def __init__(self,
               prop_names: Collection[str],
               extrinsics: Mapping[str, np.ndarray],
               limits: types.PositionLimit,
               deadzones: Optional[Mapping[str, types.PositionLimit]] = None,
               fuse_tolerance: float = 0.1,
               base_frame: str = "base",
               input_queue_size: int = 1,
               output_queue_size: int = 1,
               rate: int = 20):
    """Constructs a `BlobTriangulationNode` instance.

    Args:
      prop_names: The names of the props to use.
      extrinsics: A mapping from camera names to 7D pose vector of the camera
        realtive to a common reference frame. The 7D vector is the union of a
        position vector [x, y, z] and a quaternion [x, y, z, w].
      limits: The robot playground limits, specified in terms of upper and lower
        positions, i.e. a cuboid.
      deadzones: A mapping specifying deadzones with their limits, specified in
        the same terms of `limits`.
      fuse_tolerance: Maximum time interval between fused data points.
      base_frame: The frame id to use when publishing poses.
      input_queue_size: The size of input queues.
      output_queue_size: The size of output queues.
      rate: The frequency with which to spin the node.
    """

    self._prop_names = prop_names
    self._camera_names = list(extrinsics.keys())
    self._extrinsics = extrinsics
    self._pose_validator = utils.PoseValidator(
        limits=limits, deadzones=deadzones)
    self._fuse_tolerance = fuse_tolerance
    self._base_frame = base_frame
    self._input_queue_size = input_queue_size
    self._output_queue_size = output_queue_size
    self._rate = rospy.Rate(rate)
    self._pose_publishers = {}

    # Setup subscribers for receiving camera info.
    self._camera_info_handler = {}
    for camera_name in self._camera_names:
      camera_info_topic = f"{camera_name}/camera_info"
      self._camera_info_handler[camera_name] = ros_utils.CameraInfoHandler(
          topic=camera_info_topic, queue_size=input_queue_size)

    # Setup subscribers for receiving blob centers.
    self._point_handler = collections.defaultdict(dict)
    for prop_name in self._prop_names:
      for camera_name in self._camera_names:
        point_topic = f"{camera_name}/blob/{prop_name}/center"
        self._point_handler[prop_name][camera_name] = ros_utils.PointHandler(
            topic=point_topic, queue_size=input_queue_size)

  def spin(self) -> None:
    """Loops the node until shutdown."""
    while not rospy.is_shutdown():
      camera_matrices, distortions = self._get_camera_info()
      centers, most_recent_stamp = self._get_blob_centers()
      poses = self._fuse(centers, camera_matrices, distortions)
      self._publish_poses(poses, most_recent_stamp)
      self._rate.sleep()

  def _fuse(self, centers: Mapping[str, Mapping[str, np.ndarray]],
            camera_matrices: Mapping[str, np.ndarray],
            distortions: Mapping[str, np.ndarray]) -> Mapping[str, np.ndarray]:
    """Fuse the detected center points by triangulation."""
    prop_poses = {}
    for prop_name in self._prop_names:
      # Skip, if there's no data for the prop.
      if prop_name not in centers:
        continue
      # List the cameras in which the prop is visible.
      available_cameras = list(centers[prop_name].keys())
      # If the prop is visible in less than two cameras, skip.
      if len(available_cameras) < 2:
        continue
      available_cameras_powerset = self._powerset(
          available_cameras, min_cardinality=2)
      position = None
      residual = None
      for camera_set in available_cameras_powerset:
        # Setup the triangulation module for the camera subset.
        triangulation = linear_triangulation.Triangulation(
            camera_matrices=[camera_matrices[name] for name in camera_set],
            distortions=[distortions[name] for name in camera_set],
            extrinsics=[self._extrinsics[name] for name in camera_set])
        # Create a list of blob centers ordered by source camera.
        blob_centers = [
            centers[prop_name][camera_name] for camera_name in camera_set
        ]
        # Triangulate the prop's position.
        current_position, current_residual = triangulation.triangulate(
            blob_centers)
        if residual is None or current_residual < residual:
          position = current_position
          residual = current_residual
      # Append a default orientation.
      prop_poses[prop_name] = np.append(position, [0, 0, 0, 1])
    return prop_poses

  def _powerset(self, iterable, min_cardinality=1, max_cardinality=None):
    """Creates an iterable with all the powerset elements of `iterable`.

    Example:
      The powerset of the list [1,2,3] is (1,), (2,), (3,), (1, 2), (1, 3),
      (2, 3), (1, 2, 3).

    Args:
      iterable: An `iterable` object.
      min_cardinality: an `int`. The minimum cardinality in the powerset.
      max_cardinality: an `int`. The minimum cardinality in the powerset.

    Returns:
      An `iterable` of the powerset elements as tuples.
    """
    if max_cardinality is None:
      max_cardinality = len(iterable)
    if min_cardinality > max_cardinality:
      raise ValueError("The minimum cardinality of a pawerset cannot be "
                       " greater than its maximum cardinality. "
                       f"Provided minimum: {min_cardinality}. "
                       f"Provided maximum: {max_cardinality}")

    iterable_list = list(iterable)
    return itertools.chain.from_iterable(
        itertools.combinations(iterable_list, r)
        for r in range(min_cardinality, max_cardinality + 1))

  def _get_camera_info(
      self) -> Tuple[Mapping[str, np.ndarray], Mapping[str, np.ndarray]]:
    """Collect camera matrices and distortions of all cameras."""
    camera_matrices = {}
    distortions = {}
    for camera_name in self._camera_names:
      camera_info = self._camera_info_handler[camera_name]
      with camera_info:
        if np.count_nonzero(camera_info.matrix) == 0:
          raise ValueError(
              f'Received an all-zero camera matrix for camera {camera_name}. '
              "Please restart the camera driver, and check the camera's "
              'calibration file if the issue persists.')
        camera_matrices[camera_name] = camera_info.matrix
        distortions[camera_name] = camera_info.distortions
    return camera_matrices, distortions

  def _get_blob_centers(
      self
  ) -> Tuple[Mapping[str, Mapping[str, np.ndarray]], Optional[rospy.Time]]:
    """Get the most recent, timely coherent set of center points."""
    points_and_stamps = collections.defaultdict(dict)
    most_recent_stamp = None
    # Collect all centers and track their timestamps.
    for prop_name in self._prop_names:
      for camera_name in self._camera_names:
        point_data = self._point_handler[prop_name][camera_name].point_data
        if point_data is not None:
          points_and_stamps[prop_name][camera_name] = (point_data.data[:2],
                                                       point_data.stamp)
          if most_recent_stamp is None or point_data.stamp > most_recent_stamp:
            most_recent_stamp = point_data.stamp
        else:
          logging.warning("No data received yet ('%s', '%s').", camera_name,
                          prop_name)
          continue
    # No blob center received yet.
    if most_recent_stamp is None:
      return {}, most_recent_stamp
    # Remove outdated points.
    filtered_points = collections.defaultdict(dict)
    for prop_name, cameras in points_and_stamps.items():
      for camera_name, info_tuple in cameras.items():
        center, stamp = info_tuple
        if (most_recent_stamp - stamp).to_sec() > self._fuse_tolerance:
          logging.warning("Discarding outdated data ('%s', '%s').", camera_name,
                          prop_name)
          continue
        filtered_points[prop_name][camera_name] = center
    return filtered_points, most_recent_stamp

  def _publish_poses(self, poses: Mapping[str, np.ndarray],
                     stamp: rospy.Time) -> None:
    for prop_name, pose in poses.items():
      if pose is not None:
        if not self._pose_validator.is_valid(pose):
          continue
        if prop_name not in self._pose_publishers:
          self._setup_pose_publisher(prop_name, "pose")
        self._pose_publishers[prop_name].publish(pose, stamp=stamp)

  def _setup_pose_publisher(self, prop_name: str, topic: str) -> None:
    topic = f"/blob/{prop_name}/{topic}"
    self._pose_publishers[prop_name] = ros_utils.PosePublisher(
        topic=topic,
        frame_id=self._base_frame,
        queue_size=self._output_queue_size)
