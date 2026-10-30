#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>
#include <opencv2/imgproc.hpp>
#include "tasks/auto_aim/solver.hpp"

using Json=nlohmann::json;
int main(int argc,char **argv) {
  if(argc!=4) {std::cerr<<"usage: diagnostic_pose_prior config input.jsonl output.jsonl\n";return 2;}
  auto_aim::Solver solver(argv[1]);
  std::ifstream input(argv[2]);std::ofstream output(argv[3]);
  std::string line;
  while(std::getline(input,line)) {
    const auto row=Json::parse(line);const auto &frame=row.at("frame");
    const auto q=frame.at("q_gimbal2world").get<std::vector<double>>();
    solver.set_R_gimbal2world(Eigen::Quaterniond(q[3],q[0],q[1],q[2]).normalized());
    const auto r=frame.at("calibration").at("r_camera2gimbal").get<std::vector<double>>();
    const auto t=frame.at("calibration").at("t_camera2gimbal").get<std::vector<double>>();
    solver.set_camera2gimbal(Eigen::Map<const Eigen::Matrix<double,3,3,Eigen::RowMajor>>(r.data()),
      Eigen::Map<const Eigen::Vector3d>(t.data()));
    const auto &observation=row.at("observation");std::vector<cv::Point2f> points;
    for(const auto &p:observation.at("corners"))points.emplace_back(p[0].get<float>(),p[1].get<float>());
    int class_id=-1;
    for(size_t i=0;i<auto_aim::armor_properties.size();++i) {
      const auto &[color,name,type]=auto_aim::armor_properties[i];
      if(auto_aim::COLORS[color]==observation.at("color") &&
         auto_aim::ARMOR_NAMES[name]==observation.at("name") &&
         auto_aim::ARMOR_TYPES[type]==observation.at("type")) {class_id=i;break;}
    }
    if(class_id<0)throw std::runtime_error("Unknown observed armor class");
    Json result={{"id",row.at("id")}};
    for(const bool use_hint:{false,true}) {
      auto_aim::Armor armor(class_id,observation.at("confidence").get<float>(),cv::boundingRect(points),points);
      solver.solve(armor,use_hint?std::optional<double>(row.at("hint").get<double>()):std::nullopt);
      result[use_hint?"hinted":"unhinted"]={{"valid",armor.pose_valid},
        {"xyz",{armor.xyz_in_world.x(),armor.xyz_in_world.y(),armor.xyz_in_world.z()}},
        {"yaw",armor.ypr_in_world[0]},{"rms",armor.reprojection_rms_px},
        {"diagnosis",armor.diagnosis}};
    }
    output<<result.dump()<<'\n';
  }
}
