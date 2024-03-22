import functools

import jax
import jax.numpy as jnp

import jaxsim.api as js
import jaxsim.rbda
import jaxsim.typing as jtp

from .common import VelRepr


@jax.jit
def collidable_point_kinematics(
    model: js.model.JaxSimModel, data: js.data.JaxSimModelData
) -> tuple[jtp.Matrix, jtp.Matrix]:
    """
    Compute the position and 3D velocity of the collidable points in the world frame.

    Args:
        model: The model to consider.
        data: The data of the considered model.

    Returns:
        The position and velocity of the collidable points in the world frame.

    Note:
        The collidable point velocity is the plain coordinate derivative of the position.
        If we attach a frame C = (p_C, [C]) to the collidable point, it corresponds to
        the linear component of the mixed 6D frame velocity.
    """

    from jaxsim.rbda import collidable_points

    with data.switch_velocity_representation(VelRepr.Inertial):
        W_p_Ci, W_ṗ_Ci = collidable_points.collidable_points_pos_vel(
            model=model,
            base_position=data.base_position(),
            base_quaternion=data.base_orientation(dcm=False),
            joint_positions=data.joint_positions(model=model),
            base_linear_velocity=data.base_velocity()[0:3],
            base_angular_velocity=data.base_velocity()[3:6],
            joint_velocities=data.joint_velocities(model=model),
        )

    return W_p_Ci, W_ṗ_Ci


@jax.jit
def collidable_point_positions(
    model: js.model.JaxSimModel, data: js.data.JaxSimModelData
) -> jtp.Matrix:
    """
    Compute the position of the collidable points in the world frame.

    Args:
        model: The model to consider.
        data: The data of the considered model.

    Returns:
        The position of the collidable points in the world frame.
    """

    return collidable_point_kinematics(model=model, data=data)[0]


@jax.jit
def collidable_point_velocities(
    model: js.model.JaxSimModel, data: js.data.JaxSimModelData
) -> jtp.Matrix:
    """
    Compute the 3D velocity of the collidable points in the world frame.

    Args:
        model: The model to consider.
        data: The data of the considered model.

    Returns:
        The 3D velocity of the collidable points.
    """

    return collidable_point_kinematics(model=model, data=data)[1]


@jax.jit
def collidable_point_dynamics(
    model: js.model.JaxSimModel, data: js.data.JaxSimModelData
) -> tuple[jtp.Matrix, jtp.Matrix]:
    r"""
    Compute the 6D force applied to each collidable point and the corresponding
    material deformation rate.

    Args:
        model: The model to consider.
        data: The data of the considered model.

    Returns:
        The 6D force applied to each collidable point and the corresponding
        material deformation rate.

    Note:
        The material deformation rate is always returned in the mixed frame
        `C[W] = ({}^W \mathbf{p}_C, [W])`. This is convenient for integration purpose.
        Instead, the 6D forces are returned in the active representation.
    """

    # Compute the position and linear velocities (mixed representation) of
    # all collidable points belonging to the robot.
    W_p_Ci, W_ṗ_Ci = js.contact.collidable_point_kinematics(model=model, data=data)

    # Build the soft contact model.
    soft_contacts = jaxsim.rbda.SoftContacts(
        parameters=data.soft_contacts_params, terrain=model.terrain
    )

    # Compute the 6D force expressed in the inertial frame and applied to each
    # collidable point, and the corresponding material deformation rate.
    # Note that the material deformation rate is always returned in the mixed frame
    # C[W] = (W_p_C, [W]). This is convenient for integration purpose.
    W_f_Ci, CW_ṁ = jax.vmap(soft_contacts.contact_model)(
        W_p_Ci, W_ṗ_Ci, data.state.soft_contacts.tangential_deformation
    )

    # Convert the 6D forces to the active representation.
    f_Ci = jax.vmap(
        lambda W_f_C: data.inertial_to_other_representation(
            array=W_f_C,
            other_representation=data.velocity_representation,
            transform=data.base_transform(),
            is_force=True,
        )
    )(W_f_Ci)

    return f_Ci, CW_ṁ


@functools.partial(jax.jit, static_argnames=["link_names"])
def in_contact(
    model: js.model.JaxSimModel,
    data: js.data.JaxSimModelData,
    *,
    link_names: tuple[str, ...] | None = None,
) -> jtp.Vector:
    """
    Return whether the links are in contact with the terrain.

    Args:
        model: The model to consider.
        data: The data of the considered model.
        link_names:
            The names of the links to consider. If None, all links are considered.

    Returns:
        A boolean vector indicating whether the links are in contact with the terrain.
    """

    link_names = link_names if link_names is not None else model.link_names()

    if set(link_names) - set(model.link_names()) != set():
        raise ValueError("One or more link names are not part of the model")

    W_p_Ci = collidable_point_positions(model=model, data=data)

    terrain_height = jax.vmap(lambda x, y: model.terrain.height(x=x, y=y))(
        W_p_Ci[:, 0], W_p_Ci[:, 1]
    )

    below_terrain = W_p_Ci[:, 2] <= terrain_height

    links_in_contact = jax.vmap(
        lambda link_index: jnp.where(
            model.kin_dyn_parameters.contact_parameters.body == link_index,
            below_terrain,
            jnp.zeros_like(below_terrain, dtype=bool),
        ).any()
    )(jnp.arange(model.number_of_links()))

    return links_in_contact


@jax.jit
def estimate_good_soft_contacts_parameters(
    model: js.model.JaxSimModel,
    *,
    standard_gravity: jtp.FloatLike = jaxsim.math.StandardGravity,
    static_friction_coefficient: jtp.FloatLike = 0.5,
    number_of_active_collidable_points_steady_state: jtp.IntLike = 1,
    damping_ratio: jtp.FloatLike = 1.0,
    max_penetration: jtp.FloatLike | None = None,
) -> jaxsim.rbda.soft_contacts.SoftContactsParams:
    """
    Estimate good soft contacts parameters for the given model.

    Args:
        model: The model to consider.
        standard_gravity: The standard gravity constant.
        static_friction_coefficient: The static friction coefficient.
        number_of_active_collidable_points_steady_state:
            The number of active collidable points in steady state supporting
            the weight of the robot.
        damping_ratio: The damping ratio.
        max_penetration:
            The maximum penetration allowed in steady state when the robot is
            supported by the configured number of active collidable points.

    Returns:
        The estimated good soft contacts parameters.

    Note:
        This method provides a good starting point for the soft contacts parameters.
        The user is encouraged to fine-tune the parameters based on the
        specific application.
    """

    def estimate_model_height(model: js.model.JaxSimModel) -> jtp.Float:
        """"""

        zero_data = js.data.JaxSimModelData.build(
            model=model,
            soft_contacts_params=jaxsim.rbda.soft_contacts.SoftContactsParams(),
        )

        W_pz_CoM = js.com.com_position(model=model, data=zero_data)[2]

        if model.floating_base():
            W_pz_C = collidable_point_positions(model=model, data=zero_data)[:, -1]
            return 2 * (W_pz_CoM - W_pz_C.min())

        return 2 * W_pz_CoM

    max_δ = (
        max_penetration
        if max_penetration is not None
        else 0.005 * estimate_model_height(model=model)
    )

    nc = number_of_active_collidable_points_steady_state

    sc_parameters = (
        jaxsim.rbda.soft_contacts.SoftContactsParams.build_default_from_jaxsim_model(
            model=model,
            standard_gravity=standard_gravity,
            static_friction_coefficient=static_friction_coefficient,
            max_penetration=max_δ,
            number_of_active_collidable_points_steady_state=nc,
            damping_ratio=damping_ratio,
        )
    )

    return sc_parameters
