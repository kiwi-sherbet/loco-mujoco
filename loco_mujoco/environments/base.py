import os
import warnings
from tempfile import mkdtemp

import mujoco
from dm_control import mjcf

from mushroom_rl.environments import MultiMuJoCo
from mushroom_rl.utils import spaces
from mushroom_rl.utils.running_stats import *
from mushroom_rl.utils.mujoco import *

from loco_mujoco.utils import Trajectory
from loco_mujoco.utils import NoReward, CustomReward,\
    TargetVelocityReward, PosReward


class BaseEnv(MultiMuJoCo):
    """
    Base class for all kinds of locomotion environments.

    """

    def __init__(self, xml_path, action_spec, observation_spec, collision_groups=None, gamma=0.99, horizon=1000,
                 n_substeps=10,  reward_type=None, reward_params=None, traj_params=None, random_start=True,
                 init_step_no=None, timestep=0.001, use_foot_forces=True, default_camera_mode="follow",
                 **viewer_params):
        """
        Constructor.

        Args:
            xml_path (string): The path to the XML file with which the environment should be created;
            actuation_spec (list): A list specifying the names of the joints
                which should be controllable by the agent. Can be left empty
                when all actuators should be used;
            observation_spec (list): A list containing the names of data that
                should be made available to the agent as an observation and
                their type (ObservationType). They are combined with a key,
                which is used to access the data. An entry in the list
                is given by: (key, name, type). The name can later be used
                to retrieve specific observations;
            collision_groups (list, None): A list containing groups of geoms for
                which collisions should be checked during simulation via
                ``check_collision``. The entries are given as:
                ``(key, geom_names)``, where key is a string for later
                referencing in the "check_collision" method, and geom_names is
                a list of geom names in the XML specification;
            gamma (float): The discounting factor of the environment;
            horizon (int): The maximum horizon for the environment;
            n_substeps (int): The number of substeps to use by the MuJoCo
                simulator. An action given by the agent will be applied for
                n_substeps before the agent receives the next observation and
                can act accordingly;
            reward_type (string): Type of reward function to be used.
            reward_params (dict): Dictionary of parameters corresponding to
                the chosen reward function;
            traj_params (dict): Dictionrary of parameters to construct trajectories.
            random_start (bool): If True, a random sample from the trajectories
                is chosen at the beginning of each time step and initializes the
                simulation according to that. This requires traj_params to be passed!
            init_step_no (int): If set, the respective sample from the trajectories
                is taken to initialize the simulation;
            timestep (float): The timestep used by the MuJoCo simulator. If None, the
                default timestep specified in the XML will be used;
            use_foot_forces (bool): If True, foot forces are computed and added to
                the observation space;

        """

        if type(xml_path) != list:
            xml_path = [xml_path]

        if collision_groups is None:
            collision_groups = list()

        super().__init__(xml_path, action_spec, observation_spec, gamma=gamma, horizon=horizon,
                         n_substeps=n_substeps, timestep=timestep, collision_groups=collision_groups,
                         default_camera_mode=default_camera_mode, **viewer_params)

        # specify reward function
        self._reward_function = self._get_reward_function(reward_type, reward_params)

        # optionally use foot forces in the observation space
        self._use_foot_forces = use_foot_forces

        self.info.observation_space = spaces.Box(*self._get_observation_space())

        # the action space is supposed to be between -1 and 1, so we normalize it
        low, high = self.info.action_space.low.copy(), self.info.action_space.high.copy()
        self.norm_act_mean = (high + low) / 2.0
        self.norm_act_delta = (high - low) / 2.0
        self.info.action_space.low[:] = -1.0
        self.info.action_space.high[:] = 1.0

        # mask to get kinematic observations (-2 for neglecting x and z)
        self._kinematic_obs_mask = np.arange(len(observation_spec) - 2)

        # setup a running average window for the mean ground forces
        self.mean_grf = self._setup_ground_force_statistics()

        if traj_params:
            self.trajectories = None
            self.load_trajectory(traj_params)
        else:
            self.trajectories = None

        self._random_start = random_start
        self._init_step_no = init_step_no

    def load_trajectory(self, traj_params):
        """
        Loads trajectories. If there were trajectories loaded already, this function overrides the latter.

        Args:
            traj_params (dict): Dictionary of parameters needed to load trajectories;

        """

        if self.trajectories is not None:
            warnings.warn("New trajectories loaded, which overrides the old ones.", RuntimeWarning)

        self.trajectories = Trajectory(keys=self.get_all_observation_keys(),
                                       low=self.info.observation_space.low,
                                       high=self.info.observation_space.high,
                                       joint_pos_idx=self.obs_helper.joint_pos_idx,
                                       interpolate_map=self._interpolate_map,
                                       interpolate_remap=self._interpolate_remap,
                                       interpolate_map_params=self._get_interpolate_map_params(),
                                       interpolate_remap_params=self._get_interpolate_remap_params(),
                                       **traj_params)

    def reward(self, state, action, next_state, absorbing):
        """
        Calls the reward function of the environment.

        """

        return self._reward_function(state, action, next_state, absorbing)

    def setup(self, obs):
        """
        Function to setup the initial state of the simulation. Initialization can be done either
        randomly, from a certain initial, or from the default initial state of the model.

        Args:
            obs (np.array): Observation to initialize the environment from;

        """

        self._reward_function.reset_state()

        if obs is not None:
            self._init_sim_from_obs(obs)
        else:
            if not self.trajectories and self._random_start:
                raise ValueError("Random start not possible without trajectory data.")
            elif not self.trajectories and self._init_step_no is not None:
                raise ValueError("Setting an initial step is not possible without trajectory data.")
            elif self._init_step_no is not None and self._random_start:
                raise ValueError("Either use a random start or set an initial step, not both.")

            if self.trajectories is not None:
                if self._random_start:
                    sample = self.trajectories.reset_trajectory()
                    self.set_sim_state(sample)
                elif self._init_step_no:
                    traj_len = self.trajectories.trajectory_length
                    n_traj = self.trajectories.nnumber_of_trajectories
                    assert self._init_step_no <= traj_len * n_traj
                    substep_no = int(self._init_step_no % traj_len)
                    traj_no = int(self._init_step_no / traj_len)
                    sample = self.trajectories.reset_trajectory(substep_no, traj_no)
                    self.set_sim_state(sample)

    def is_absorbing(self, obs):
        """
        Checks if an observation is an absorbing state or not.

        Args:
            obs (np.array): Current observation;

        Returns:
            True, if the observation is an absorbing state; otherwise False;

        """

        return self._has_fallen(obs)

    def get_kinematic_obs_mask(self):
        """
        Returns a mask (np.array) for the observation specified in observation_spec (or part of it).

        """

        return self._kinematic_obs_mask

    def obs_to_kinematics_conversion(self, obs):
        """
        Calculates a dictionary containing the kinematics given the observation.

        Args:
            obs (np.array): Current observation;

        Returns:
            Dictionary containing the keys specified in observation_spec with the corresponding
            values from the observation.

        """

        obs = np.atleast_2d(obs)
        rel_keys = [obs_spec[0] for obs_spec in self.obs_helper.observation_spec]
        num_data = len(obs)
        dataset = dict()
        for i, key in enumerate(rel_keys):
            if i < 2:
                # fill with zeros for x and y position
                data = np.zeros(num_data)
            else:
                data = obs[:, i-2]
            dataset[key] = data

        return dataset

    def get_obs_idx(self, key):
        """
        Returns a list of indices corresponding to the respective key.

        """

        idx = self.obs_helper.obs_idx_map[key]

        # shift by 2 to account for deleted x and y
        idx = [i-2 for i in idx]

        return idx

    def create_dataset(self, ignore_keys=None):
        """
        Creates a dataset from the specified trajectories.

        Args:
            ignore_keys (list): List of keys to ignore in the dataset.

        Returns:
            Dictionary containing states, next_states and absorbing flags. For the states the shape is
            (N_traj x N_samples_per_traj, dim_state), while the absorbing flag has the shape is
            (N_traj x N_samples_per_traj).

        """

        if self.trajectories is not None:
            return self.trajectories.create_dataset(ignore_keys=ignore_keys)
        else:
            raise ValueError("No trajectory was passed to the environment. "
                             "To create a dataset pass a trajectory first.")

    def play_trajectory_demo(self):
        """
        Plays a demo of the loaded trajectory by forcing the model
        positions to the ones in the trajectories at every step.

        """

        assert self.trajectories is not None

        sample = self.trajectories.reset_trajectory(substep_no=1)
        self.set_sim_state(sample)
        while True:
            sample = self.trajectories.get_next_sample()

            self.set_sim_state(sample)

            mujoco.mj_forward(self._model, self._data)

            obs = self._create_observation(sample)
            if self._has_fallen(obs):
                print("Has fallen!")

            self.render()

    def play_trajectory_demo_from_velocity(self):
        """
        Plays a demo of the loaded trajectory by forcing the model
        positions to the ones in the trajectories at every step.

        """

        assert self.trajectories is not None

        sample = self.trajectories.reset_trajectory(substep_no=1)
        self.set_sim_state(sample)
        len_qpos, len_qvel = self._len_qpos_qvel()
        # todo: adapt this to new trajectory format.
        curr_qpos = sample[0:len_qpos]
        while True:

            sample = self.trajectories.get_next_sample()
            qvel = sample[len_qpos:len_qpos + len_qvel]
            qpos = curr_qpos + self.dt * qvel
            sample[:len(qpos)] = qpos

            self.set_sim_state(sample)

            mujoco.mj_forward(self._model, self._data)

            # save current qpos
            curr_qpos = self._get_joint_pos()

            obs = self._create_observation(sample)
            if self._has_fallen(obs):
                print("Has fallen!")

            self.render()

    def set_sim_state(self, sample):
        """
        Sets the state of the simulation according to an observation.

        Args:
            sample (list or np.array): Sample used to set the state of the simulation.

        """

        obs_spec = self.obs_helper.observation_spec
        assert len(sample) == len(obs_spec)

        for key_name_ot, value in zip(obs_spec, sample):
            key, name, ot = key_name_ot
            if ot == ObservationType.JOINT_POS:
                self._data.joint(name).qpos = value
            elif ot == ObservationType.JOINT_VEL:
                self._data.joint(name).qvel = value
            elif ot == ObservationType.SITE_ROT:
                assert len(value.shape) == 2
                self._data.site(name).xmat = value

    def _get_observation_space(self):
        """
        Returns a tuple of the lows and highs (np.array) of the observation space.

        """

        sim_low, sim_high = (self.info.observation_space.low[2:],
                             self.info.observation_space.high[2:])

        if self._use_foot_forces:
            grf_low, grf_high = (-np.ones((12,)) * np.inf,
                                 np.ones((12,)) * np.inf)
            return (np.concatenate([sim_low, grf_low]),
                    np.concatenate([sim_high, grf_high]))
        else:
            return sim_low, sim_high

    def _create_observation(self, obs):
        """
        Creates a full vector of observations.

        Args:
            obs (np.array): Observation vector to be modified or extended;

        Returns:
            New observation vector (np.array);

        """

        if self._use_foot_forces:
            obs = np.concatenate([obs[2:],
                                  self.mean_grf.mean / 1000.,
                                  ]).flatten()
        else:
            obs = np.concatenate([obs[2:],
                                  ]).flatten()

        return obs

    def _preprocess_action(self, action):
        """
        This function preprocesses all actions. All actions in this environment expected to be between -1 and 1.
        Hence, we need to unnormalize the action to send to correct action to the simulation.
        Note: If the action is not in [-1, 1], the unnormalized version will be clipped in Mujoco.

        Args:
            action (np.array): Action to be send to the environment;

        Returns:
            Unnormalized action (np.array) that is send to the environment;

        """

        unnormalized_action = ((action.copy() * self.norm_act_delta) + self.norm_act_mean)
        return unnormalized_action

    def _simulation_post_step(self):
        """
        Update the ground forces statistics if needed.

        """

        if self._use_foot_forces:
            grf =self._get_ground_forces()
            self.mean_grf.update_stats(grf)

    def _init_sim_from_obs(self, obs):
        """
        Initializes the simulation from an observation.

        Args:
            obs (np.array): The observation to set the simulation state to.

        """

        assert len(obs.shape) == 1

        # append x and y pos
        obs = np.concatenate([[0.0, 0.0], obs])

        obs_spec = self.obs_helper.observation_spec
        assert len(obs) >= len(obs_spec)

        # remove anything added to obs that is not in obs_spec
        obs = obs[:len(obs_spec)]

        # set state
        self.set_sim_state(obs)

    def _setup_ground_force_statistics(self):
        """
        Returns a running average method for the mean ground forces.  By default, 4 ground force sensors are used.
        Environments that use more or less have to override this function.

        """

        mean_grf = RunningAveragedWindow(shape=(self._get_grf_size(),), window_size=self._n_substeps)

        return mean_grf

    def _get_ground_forces(self):
        """
        Returns the ground forces (np.array). By default, 4 ground force sensors are used.
        Environments that use more or less have to override this function.

        """

        grf = np.concatenate([self._get_collision_force("floor", "foot_r")[:3],
                              self._get_collision_force("floor", "front_foot_r")[:3],
                              self._get_collision_force("floor", "foot_l")[:3],
                              self._get_collision_force("floor", "front_foot_l")[:3]])

        return grf

    @staticmethod
    def _get_grf_size():
        """
        Returns the size of the ground force vector.

        """

        return 12

    def _get_reward_function(self, reward_type, reward_params):
        """
        Constructs a reward function.

        Args:
            reward_type (string): Name of the reward.
            reward_params (dict): Parameters of the reward function.

        Returns:
            Reward function.

        """

        if reward_type == "custom":
            reward_func = CustomReward(**reward_params)
        elif reward_type == "target_velocity":
            x_vel_idx = self.get_obs_idx("dq_pelvis_tx")
            assert len(x_vel_idx) == 1
            x_vel_idx = x_vel_idx[0]
            reward_func = TargetVelocityReward(x_vel_idx=x_vel_idx, **reward_params)
        elif reward_type == "x_pos":
            x_idx = self.get_obs_idx("q_pelvis_tx")
            assert len(x_idx) == 1
            x_idx = x_idx[0]
            reward_func = PosReward(pos_idx=x_idx)
        elif reward_type is None:
            reward_func = NoReward()
        else:
            raise NotImplementedError("The specified reward has not been implemented: ", reward_type)

        return reward_func

    def _get_joint_pos(self):
        """
        Returns a vector (np.array) containing the current joint position of the model in the simulation.

        """

        return self.obs_helper.get_joint_pos_from_obs(self.obs_helper._build_obs(self._data))

    def _get_joint_vel(self):
        """
        Returns a vector (np.array) containing the current joint velocities of the model in the simulation.

        """

        return self.obs_helper.get_joint_vel_from_obs(self.obs_helper._build_obs(self._data))

    def _get_from_obs(self, obs, keys):
        """
        Returns a part of the observation based on the specified keys.

        Args:
            obs (np.array): Observation array.
            keys (list or str): List of keys or just one key which are
                used to extract entries from the observation.

        Returns:
            np.array including the parts of the original observation whose
            keys were specified.

        """

        # obs has removed x and y positions, add dummy entries
        obs = np.concatenate([[0.0, 0.0], obs])
        if type(keys) != list:
            assert type(keys) == str
            keys = list(keys)

        entries = []
        for key in keys:
            entries.append(self.obs_helper.get_from_obs(obs, key))

        return np.concatenate(entries)

    def _len_qpos_qvel(self):
        """
        Returns the lengths of the joint position vector and the joint velocity vector, including x and y.

        """

        keys = self.get_all_observation_keys()
        len_qpos = len([key for key in keys if key.startswith("q_")])
        len_qvel = len([key for key in keys if key.startswith("dq_")])

        return len_qpos, len_qvel

    def _has_fallen(self, obs):
        """
        Checks if a model has fallen. This has to be implemented for each environment.
        
        Args:
            obs (np.array): Current observation; 

        Returns:
            True, if the model has fallen for the current observation, False otherwise.

        """
        
        raise NotImplementedError

    def _get_interpolate_map_params(self):
        """
        Returns all parameters needed to do the interpolation mapping for the respective environment.

        """

        pass

    def _get_interpolate_remap_params(self):
        """
        Returns all parameters needed to do the interpolation remapping for the respective environment.

        """

        pass

    @staticmethod
    def _interpolate_map(traj, **interpolate_map_params):
        """
        A mapping that is supposed to transform a trajectory into a space where interpolation is
        allowed. E.g., maps a rotation matrix to a set of angles. If this function is not
        overwritten, it just converts the list of np.arrays to a np.array.

        Args:
            traj (list): List of np.arrays containing each observations. Each np.array
                has the shape (n_trajectories, n_samples, (dim_observation)). If dim_observation
                is one the shape of the array is just (n_trajectories, n_samples).
            interpolate_map_params: Set of parameters needed by the individual environments.

        Returns:
            A np.array with shape (n_observations, n_trajectories, n_samples). dim_observation
            has to be one.

        """

        return np.array(traj)

    @staticmethod
    def _interpolate_remap(traj, **interpolate_remap_params):
        """
        The corresponding backwards transformation to _interpolation_map. If this function is
        not overwritten, it just converts the np.array to a list of np.arrays.

        Args:
            traj (np.array): Trajectory as np.array with shape (n_observations, n_trajectories, n_samples).
            dim_observation is one.
            interpolate_remap_params: Set of parameters needed by the individual environments.

        Returns:
            List of np.arrays containing each observations. Each np.array has the shape
            (n_trajectories, n_samples, (dim_observation)). If dim_observation
            is one the shape of the array is just (n_trajectories, n_samples).

        """

        return [obs for obs in traj]

    @staticmethod
    def _delete_from_xml_handle(xml_handle, joints_to_remove, motors_to_remove, equ_constraints):
        """
        Deletes certain joints, motors and equality constraints from a Mujoco XML handle.

        Args:
            xml_handle: Handle to Mujoco XML.
            joints_to_remove (list): List of joint names to remove.
            motors_to_remove (list): List of motor names to remove.
            equ_constraints (list): List of equality constraint names to remove.

        Returns:
            Modified Mujoco XML handle.

        """

        for j in joints_to_remove:
            j_handle = xml_handle.find("joint", j)
            j_handle.remove()
        for m in motors_to_remove:
            m_handle = xml_handle.find("actuator", m)
            m_handle.remove()
        for e in equ_constraints:
            e_handle = xml_handle.find("equality", e)
            e_handle.remove()

        return xml_handle

    @staticmethod
    def _save_xml_handle(xml_handle, tmp_dir_name, file_name="tmp_model.xml"):
        """
        Save the Mujoco XML handle to a file at tmp_dir_name. If tmp_dir_name is None,
        a temporary directory is created at /tmp.

        Args:
            xml_handle: Mujoco XML handle.
            tmp_dir_name (str): Path to temporary directory. If None, a
            temporary directory is created at /tmp.

        Returns:
            String of the save path.

        """

        if tmp_dir_name is not None:
            assert os.path.exists(tmp_dir_name), "specified directory (\"%s\") does not exist." % tmp_dir_name

        dir = mkdtemp(dir=tmp_dir_name)
        file_path = os.path.join(dir, file_name)

        # dump data
        mjcf.export_with_assets(xml_handle, dir, file_name)

        return file_path