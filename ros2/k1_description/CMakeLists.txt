cmake_minimum_required(VERSION 3.8)
project(k1_description)

find_package(ament_cmake REQUIRED)

install(
  DIRECTORY urdf meshes launch config rviz
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
