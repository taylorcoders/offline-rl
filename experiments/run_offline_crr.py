#python3
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import copy

from absl import app
from absl import flags
from tqdm import tqdm

from acme.agents.tf import actors

from acme.environment_loop import EnvironmentLoop
from acme.utils import counting
from acme.tf import utils as tf2_utils, networks
from acme import specs

import tensorflow as tf
import sonnet as snt
import tensorflow_probability as tfp
from utils import load_tf_dataset, _build_environment, _build_custom_loggers, \
    preprocess_dataset, compute_empirical_policy, init_or_resume
from visualization import evaluate_q, visualize_policy

from crr.learning import CRRLearner


# general run config
flags.DEFINE_string('environment_name', 'MiniGrid-Empty-6x6-v0', 'MiniGrid env name.')
flags.DEFINE_string('logs_tag', 'tag', 'Tag a specific run for logging in TB.')
flags.DEFINE_boolean('wandb', True, 'Whether to log results to wandb.')
flags.DEFINE_string('dataset_dir', 'datasets', 'Directory containing an offline dataset.')
flags.DEFINE_integer('evaluate_every', 100, 'Evaluation period.')
flags.DEFINE_integer('evaluation_episodes', 10, 'Evaluation episodes.')
flags.DEFINE_integer('epochs', 100, 'Number of epochs to run (samples only 1 transition per episode in each epoch).')
flags.DEFINE_integer('seed', 1234, 'Random seed for replicable results. Set to 0 for no seed.')

# general learner config
flags.DEFINE_integer('batch_size', 64, 'Batch size.')
flags.DEFINE_float('epsilon', 0.05, 'Epsilon for the epsilon greedy in the env.')
flags.DEFINE_float('learning_rate', 1e-4, 'Learning rate.')
flags.DEFINE_float('discount', 0.99, 'Discount factor.')
flags.DEFINE_integer('n_steps', 1, 'Bootstrap after n steps.')

# specific config
flags.DEFINE_float('crr_beta', 1.0, 'Param for calculating the policy improvement coefficient.')
flags.DEFINE_float('cql_alpha', 0.0, 'Scaling parameter for the offline loss regularizer.')
flags.DEFINE_string('policy_improvement_mode', 'binary', 'Defines how the advantage is processed.')
FLAGS = flags.FLAGS


def main(_):
    wb_run = init_or_resume(FLAGS)

    if FLAGS.seed:
        tf.random.set_seed(FLAGS.seed)

    # Create an environment and grab the spec.
    environment = _build_environment(FLAGS.environment_name)
    environment_spec = specs.make_environment_spec(environment)

    # Load demonstration dataset.
    raw_dataset = load_tf_dataset(directory=FLAGS.dataset_dir)
    empirical_policy = compute_empirical_policy(raw_dataset)

    dataset = preprocess_dataset(raw_dataset, FLAGS.batch_size, FLAGS.n_steps, FLAGS.discount)

    # Create the policy and critic networks.
    critic_network = snt.Sequential([
      snt.Flatten(),
      snt.nets.MLP([128, 64, 32, environment_spec.actions.num_values]),
    ])

    policy_network = snt.Sequential([
      copy.deepcopy(critic_network),
      tfp.distributions.Categorical
    ])

    behaviour_network = snt.Sequential([
      policy_network,
      networks.StochasticSamplingHead()
    ])

    # Ensure that we create the variables before proceeding (maybe not needed).
    tf2_utils.create_variables(policy_network, [environment_spec.observations])
    tf2_utils.create_variables(critic_network, [environment_spec.observations])

    # Create the actor which defines how we take actions.
    evaluation_actor = actors.FeedForwardActor(behaviour_network)

    counter = counting.Counter()
    learner_counter = counting.Counter(counter)

    disp, disp_loop = _build_custom_loggers(wb_run)

    eval_loop = EnvironmentLoop(
        environment=environment,
        actor=evaluation_actor,
        counter=counter,
        logger=disp_loop)

    learner = CRRLearner(
        policy_network=policy_network,
        critic_network=critic_network,
        dataset=dataset,
        discount=0.99,
        policy_improvement_modes=FLAGS.policy_improvement_mode,
        beta=FLAGS.crr_beta,
        cql_alpha=FLAGS.cql_alpha,
        empirical_policy=empirical_policy,
        logger=disp,
        counter=learner_counter
    )

    # Run the environment loop.
    for e in tqdm(range(FLAGS.epochs)):
        for _ in range(FLAGS.evaluate_every):
            learner.step()
        eval_loop.run(FLAGS.evaluation_episodes)
        # Visualization of the policy
        Q = evaluate_q(learner._critic_network, environment)
        plot = visualize_policy(Q, environment)
        wb_run.log({'chart': plot, 'epoch_counter': e})

    learner.save(tag=FLAGS.logs_tag)


if __name__ == '__main__':
    app.run(main)

